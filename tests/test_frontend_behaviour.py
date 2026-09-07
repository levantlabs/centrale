"""What the frontend DOES, driven over the real static/ sources.

Task-108's other half. Most of Centrale's frontend tests grep source
text (`tests/test_server.py`, through `tests/source_contract.py`) because
there is no browser dependency here; that is the right tool for a
source-shape invariant and the wrong one for a behavioural claim. An
assertion that `setInterval(` does not appear in `pane.js` is not the
claim "there is one poller": it stays green through any rewrite that
keeps the spelling, and it fires on a refactor that changes nothing.

So the claims that are actually about behaviour are made here instead,
by running the real sources under node over the DOM shim in
`tests/js_harness.py` and watching what happens: how many timers are
armed, which URLs are fetched, where a node ends up in the tree, what a
key press closes. The module is skipped whole on a machine without
`node` (see `js_harness.requires_node`) -- except under a release, where
`CENTRALE_REQUIRE_NODE` turns that skip into a failure, because a
snapshot whose JavaScript was never executed is exactly what a release
gate exists to catch (task-124; `BehaviouralTierRuntimeGateTests` below
covers the switch itself, and needs no node to do it).

The invariants below are the ones task-108's triage put in the
"deserves a behavioural test" bucket -- above all the single-poller
contract that task-73, task-93 and task-114 each restated in source
text from their own side.
"""

import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import js_harness  # noqa: E402
import server  # noqa: E402
import settings  # noqa: E402


# ---------------------------------------------------------------------
# The driver
# ---------------------------------------------------------------------

# Walks the live pane through the flows the assertions below are about:
# a poll starting and ticking, the theater opening and closing over it,
# the Escape ladder, the adaptive cadence, a reply and a stale one, and
# the two gates that keep the poll from ever starting.
#
# The clock and every timer are the driver's: `Date.now` is frozen and
# advanced by hand, and `setTimeout` records what was armed at which
# delay instead of waiting. So "one armed timer at 300ms" is an
# observation, and a second poller would show up as a second entry --
# there is nothing here that could not notice one.
PANE_DRIVER_JS = js_harness.LOAD_SOURCES_JS + r"""
var C = loadFrontend(["state.js", "dom.js", "tasks.js", "board.js",
                      "spawn.js", "harvest.js", "drawer.js", "pane.js"]);

// -- a clock the driver owns --
var NOW = 1767225600000;
Date.now = function () { return NOW; };
function advance(ms) { NOW += ms; }

// -- timers the driver owns: armed, never waited on --
var timers = [];
global.setTimeout = function (fn, ms) {
  var t = { fn: fn, ms: ms, dead: false };
  timers.push(t);
  return t;
};
global.clearTimeout = function (t) { if (t) t.dead = true; };
function armed() { return timers.filter(function (t) { return !t.dead; }); }
function armedDelays() { return armed().map(function (t) { return t.ms; }); }
function fireTimers() {
  var due = armed();
  due.forEach(function (t) { t.dead = true; });
  due.forEach(function (t) { t.fn(); });
  return settle();
}

// -- every request the page makes, in order --
var requests = [];
var bodies = [];
var CAPTURE = { lines: ["one", "two", "three"], capturedAt: "2026-09-04T10:00:00Z" };
var paneStatus = 200;
var inputStatus = 200;
global.fetch = function (url, opts) {
  var method = (opts && opts.method) || "GET";
  requests.push(method + " " + url);
  if (opts && opts.body) bodies.push(JSON.parse(opts.body));
  var status = url.indexOf("/api/session-pane") === 0 ? paneStatus
    : (url === "/api/session-input" ? inputStatus : 200);
  var data = url.indexOf("/api/session-pane") === 0 ? CAPTURE : {};
  return Promise.resolve({
    ok: status === 200, status: status,
    json: function () { return Promise.resolve(status === 200 ? data : { error: "refused" }); }
  });
};

// -- the files this harness does not load, as counting sinks --
var renders = { board: 0, sessions: 0, all: 0 };
var settingsClosed = 0;
var toasts = [];
C.renderBoard = function () { renders.board++; };
C.renderSessionsPanel = function () { renders.sessions++; };
C.doRefresh = function () {};
C.fetchSessions = function () {};
C.showToast = function (m) { toasts.push(m); };
C.openProjectBoard = function () {};
C.closeSettingsModal = function () { settingsClosed++; C.settingsOpen = false; };

wireDrawerShell();

var MINE = "TASK-77";
var OTHER = "TASK-78";

// A board with TWO live sessions in the same project, and the drawer
// open on one of them: a poller that fetched per session rather than
// per open drawer would be visible in the request list immediately.
function board(opts) {
  opts = opts || {};
  function task(id) {
    return {
      id: id, title: "A task", status: "In Progress", ready: true,
      hasSpawnBranch: true, alreadyMerged: false, worktreeDirty: false,
      branchCheckout: { kind: "centrale" }
    };
  }
  var mine = task(MINE);
  C.knownProjects.length = 0;
  C.knownProjects.push("my-tool");
  C.boardData = {
    projects: [{ name: "my-tool", tasks: [mine, task(OTHER)],
                 statuses: ["To Do", "In Progress", "Done"] }],
    capabilities: { tmux: true }
  };
  C.sessionsData = opts.noSession ? [] : [
    { name: "centrale-my-tool-" + MINE, project: "my-tool", agentState: opts.state || "waiting" },
    { name: "centrale-my-tool-" + OTHER, project: "my-tool", agentState: "working" }
  ];
  C.currentDrawer = { project: "my-tool", id: MINE, summary: mine, branchTask: null, detail: null };
  C.sessionPreviewMode = "mode" in opts ? opts.mode : "interact";
  requests.length = 0;
  bodies.length = 0;
  renders.board = renders.sessions = 0;
}

// Put the page back to "nothing open" between scenarios, through the
// real teardown path rather than by poking at state.
function reset() {
  C.settingsOpen = false;
  C.currentDrawer = null;
  C.syncDrawerPanePolling();
  timers.length = 0;
  requests.length = 0;
}

function id(el) { return el ? (el.id || el.tagName) : null; }
function areaParent() {
  var area = C.byId("drawer-pane-area");
  return area && area.parentNode ? area.parentNode.id : null;
}
function areaChildIds() {
  var area = C.byId("drawer-pane-area");
  return area ? area.childNodes.map(function (n) { return n.id || n.className; }) : null;
}
function maximizeButton() { return C.byId("drawer-pane-theater-toggle"); }
function paneText() { var pre = C.byId("drawer-pane-pre"); return pre ? pre.textContent : null; }
function replyStatus() {
  var el = C.byId("drawer-pane-reply-status");
  return el ? el.textContent : null;
}
function sendReply(text) {
  var input = C.byId("drawer-pane-reply-input");
  input.value = text;
  C.byId("drawer-pane-reply-send").click();
  return settle();
}
function keyButtons() {
  var row = C.byId("drawer-pane-reply");
  return row ? row.querySelectorAll(".drawer-pane-reply-key") : [];
}
// Named for the button, not the keystroke: the shim's own global
// pressKey() presses a key on the DOCUMENT (the Escape ladder), and a
// function declaration here would be overwritten by it at load.
function clickSessionKey(label) {
  keyButtons().filter(function (b) { return b.textContent === label; })[0].click();
  return settle();
}

var out = {};

// 1. A poll starts for the open drawer's task, ticks on one timer, and
//    never re-renders the board.
function pollLifecycle() {
  board();
  C.syncDrawerPanePolling();
  return settle().then(function () {
    out.afterStart = {
      requests: requests.slice(), armed: armedDelays(),
      area: areaChildIds(), text: paneText()
    };
    return fireTimers();
  }).then(fireTimers).then(function () {
    out.afterTwoTicks = { requests: requests.slice(), armed: armedDelays() };
    out.rendersDuringPolling = { board: renders.board, sessions: renders.sessions };
  });
}

// 1b. A drawer render never disturbs the pane section, and a capture is
//     clamped before it is rendered (task-50 / task-60).
function drawerRendersLeaveThePaneAlone() {
  var beforeArea = areaChildIds();
  C.renderDrawerSessionArea();
  C.renderDrawerSpawnArea();
  C.renderDrawerHarvestArea();
  out.afterDrawerRenders = {
    area: areaChildIds(), armed: armedDelays(),
    text: paneText(), requests: requests.length,
    unchanged: JSON.stringify(beforeArea) === JSON.stringify(areaChildIds())
  };
  // A full-width TUI rule and a run of real content of the same length:
  // the rule is collapsed to 40, the word is not touched.
  CAPTURE = { lines: [new Array(61).join("\u2500"), new Array(61).join("x")],
              capturedAt: "2026-09-04T10:00:05Z" };
  return fireTimers().then(function () {
    out.clamped = paneText().split("\n").map(function (line) { return line.length; });
    CAPTURE = { lines: ["one", "two", "three"], capturedAt: "2026-09-04T10:00:00Z" };
    return fireTimers();
  });
}

// 2. The theater MOVES the one pane node. No copy, no second timer, no
//    fetch of its own -- it re-renders from the payload already in hand.
function theaterMovesTheOnePane() {
  var before = requests.length;
  var area = C.byId("drawer-pane-area");
  // A reader parked partway up a long capture: Maximize must land on the
  // newest output (task-114), and closing must put the drawer back where
  // it was rather than mapping the theater's offset into a shorter
  // document.
  var pre = C.byId("drawer-pane-pre");
  pre.scrollHeight = 5000;
  pre.scrollTop = 120;
  maximizeButton().click();
  out.theaterScroll = { onOpen: C.byId("drawer-pane-pre").scrollTop };
  out.theaterOpen = {
    newRequests: requests.length - before,
    armed: armedDelays(),
    areaParent: areaParent(),
    sameNode: area === C.byId("drawer-pane-area"),
    paneNodes: countById("drawer-pane-pre"),
    areaNodes: countById("drawer-pane-area"),
    railNodes: countById("theater-rail"),
    label: maximizeButton().textContent,
    theaterClasses: C.byId("theater").className,
    ariaHidden: C.byId("theater").getAttribute("aria-hidden")
  };
  var beforeClose = requests.length;
  maximizeButton().click();
  out.theaterClosed = {
    newRequests: requests.length - beforeClose,
    armed: armedDelays(),
    areaParent: areaParent(),
    paneNodes: countById("drawer-pane-pre"),
    railNodes: countById("theater-rail"),
    label: maximizeButton().textContent,
    theaterClasses: C.byId("theater").className
  };
  out.theaterScroll.onClose = C.byId("drawer-pane-pre").scrollTop;
  return settle();
}

// 3. One Escape closes ONE layer, outermost first, and the drawer
//    closing takes the poll and the pane section with it.
function escapeLadder() {
  maximizeButton().click();          // theater open again
  C.settingsOpen = true;
  pressKey("Escape");
  out.escapeWithSettingsOpen = {
    settingsClosed: settingsClosed,
    theaterOpen: C.byId("theater").className.indexOf("open") !== -1,
    drawerOpen: !!C.currentDrawer
  };
  pressKey("Escape");
  out.escapeClosesTheater = {
    theaterOpen: C.byId("theater").className.indexOf("open") !== -1,
    drawerOpen: !!C.currentDrawer,
    armed: armedDelays(),
    areaParent: areaParent()
  };
  pressKey("Escape");
  return settle().then(function () {
    out.escapeClosesDrawer = {
      drawerOpen: !!C.currentDrawer,
      armed: armedDelays(),
      area: areaChildIds(),
      areaParent: areaParent(),
      drawerClasses: C.byId("drawer").className
    };
  });
}

// 4. Three cadences on the SAME one timer (task-114).
function cadence() {
  reset();
  board({ state: "waiting" });
  C.syncDrawerPanePolling();
  return settle().then(function () {
    out.cadenceIdle = armedDelays();
    C.sessionsData[0].agentState = "working";
    return fireTimers();
  }).then(function () {
    out.cadenceWorking = armedDelays();
    return sendReply("yes");
  }).then(function () {
    out.afterSend = {
      armed: armedDelays(),
      requests: requests.slice(),
      body: bodies[bodies.length - 1],
      status: replyStatus()
    };
    advance(5000);                    // the burst expires on its own
    return fireTimers();
  }).then(function () {
    out.cadenceAfterBurst = armedDelays();
  });
}

// 4b. Leaving the theater ends the fast phase at once, and a 403 on the
//     reply drops to the read-only tier without stopping the pane.
function burstEndsWithTheTheaterAnd403DropsTheTier() {
  maximizeButton().click();                 // reply from inside the theater
  return sendReply("again").then(function () {
    out.burstInTheater = armedDelays();
    maximizeButton().click();               // closing ends the burst at once
    out.burstAfterLeavingTheater = armedDelays();
    inputStatus = 403;
    return sendReply("refused");
  }).then(function () {
    out.after403 = {
      mode: C.sessionPreviewMode,
      hasReplyRow: !!C.byId("drawer-pane-reply"),
      hasPane: !!C.byId("drawer-pane-pre"),
      armed: armedDelays(),
      toast: toasts[toasts.length - 1]
    };
    inputStatus = 200;
  });
}

// 5. A capture that has gone stale blocks the send client-side -- no
//    POST leaves the page at all.
function staleCaptureBlocksTheSend() {
  reset();
  board();
  C.syncDrawerPanePolling();
  return settle().then(function () {
    advance(15000);
    C.updateDrawerPaneAge();          // the 1s clock in api.js, by hand
    out.staleGate = { status: replyStatus(), sendDisabled: C.byId("drawer-pane-reply-send").disabled };
    return sendReply("hello");
  }).then(function () {
    out.staleSend = {
      posts: requests.filter(function (r) { return r.indexOf("POST") === 0; }),
      status: replyStatus()
    };
  });
}

// 5b. task-135: the two session keys. The claim the source shape cannot
//     make is what LEAVES the page when one is clicked -- a {"key"} body
//     with no "text" in it, an untouched draft in the input, and the same
//     post-send behaviour (burst, immediate re-capture) a text reply gets.
//     Sitting on a startup modal is exactly when the draft must survive:
//     the user has already typed the reply the modal is blocking.
function sessionKeysGoAsKeysNotText() {
  reset();
  board();
  C.syncDrawerPanePolling();
  return settle().then(function () {
    out.keyButtons = keyButtons().map(function (b) {
      return { label: b.textContent, key: b.getAttribute("data-key"), title: b.title };
    });
    C.byId("drawer-pane-reply-input").value = "it looks good";  // a queued reply
    return clickSessionKey("Esc");
  }).then(function () {
    out.afterEsc = {
      body: bodies[bodies.length - 1],
      posts: requests.filter(function (r) { return r.indexOf("POST") === 0; }),
      status: replyStatus(),
      draft: C.byId("drawer-pane-reply-input").value,
      armed: armedDelays()
    };
    return clickSessionKey("Enter");
  }).then(function () {
    out.afterEnterKey = {
      body: bodies[bodies.length - 1],
      status: replyStatus(),
      draft: C.byId("drawer-pane-reply-input").value
    };
    // ...and the draft the modal was blocking now sends as ordinary text.
    return sendReply("it looks good");
  }).then(function () {
    out.afterQueuedReply = {
      body: bodies[bodies.length - 1],
      status: replyStatus(),
      draft: C.byId("drawer-pane-reply-input").value
    };
    // A key is refused by the same stale gate as text: no POST at all.
    advance(15000);
    C.updateDrawerPaneAge();
    var before = requests.length;
    return clickSessionKey("Esc").then(function () {
      out.staleKey = { newRequests: requests.length - before, status: replyStatus() };
    });
  });
}

// 6. The two gates: the feature off, and no live session. Neither ever
//    starts a poll or renders a pane section.
function gates() {
  reset();
  board({ mode: "off" });
  C.syncDrawerPanePolling();
  return settle().then(function () {
    out.tierOff = { requests: requests.slice(), armed: armedDelays(), area: areaChildIds() };
    reset();
    board({ noSession: true });
    C.syncDrawerPanePolling();
    return settle();
  }).then(function () {
    out.noLiveSession = { requests: requests.slice(), armed: armedDelays(), area: areaChildIds() };
    reset();
    board({ mode: "view" });
    C.syncDrawerPanePolling();
    return settle();
  }).then(function () {
    out.viewTier = {
      requests: requests.slice(), armed: armedDelays(),
      hasReplyRow: !!C.byId("drawer-pane-reply"),
      hasPane: !!C.byId("drawer-pane-pre")
    };
  });
}

// 7. Wide mode (task-68): the drawer's other width, from the toggle in
//    the pane header. The claim is as much about what it does NOT do --
//    no board render, no fetch, no second timer, no scroll of the pane
//    it exists to make readable -- so every one of those is read either
//    side of the click.
function wideMode() {
  reset();
  board();
  C.syncDrawerPanePolling();
  return settle().then(function () {
    var toggle = C.byId("drawer-pane-wide-toggle");
    var header = C.byId("drawer-pane-area").childNodes[0];
    out.wideToggleHome = {
      inPaneHeader: header.className === "drawer-pane-header"
        && header.querySelectorAll("button").indexOf(toggle) !== -1,
      // Its neighbour is the theater control: one header, both tiers.
      headerButtons: header.querySelectorAll("button").map(function (b) { return b.id; })
    };
    out.wideBefore = {
      label: toggle.textContent,
      pressed: toggle.getAttribute("aria-pressed"),
      title: toggle.title,
      drawerClasses: C.byId("drawer").className,
      stored: window.localStorage.getItem("centrale-drawer-wide")
    };

    var beforeRequests = requests.length;
    var beforeBoard = renders.board;
    var beforeSessions = renders.sessions;
    var pre = C.byId("drawer-pane-pre");
    pre.scrollHeight = 5000;
    pre.scrollTop = 77;
    toggle.click();
    out.wideOn = {
      drawerClasses: C.byId("drawer").className,
      label: toggle.textContent,
      pressed: toggle.getAttribute("aria-pressed"),
      title: toggle.title,
      stored: window.localStorage.getItem("centrale-drawer-wide"),
      newRequests: requests.length - beforeRequests,
      boardRenders: renders.board - beforeBoard,
      sessionRenders: renders.sessions - beforeSessions,
      armed: armedDelays(),
      paneScroll: pre.scrollTop,
      text: paneText()
    };
    toggle.click();
    out.wideOff = {
      drawerClasses: C.byId("drawer").className,
      label: toggle.textContent,
      pressed: toggle.getAttribute("aria-pressed"),
      stored: window.localStorage.getItem("centrale-drawer-wide")
    };

    // Wide, then the drawer closes: the class goes with the pane
    // section, and the preference does not.
    toggle.click();
    out.wideWhileOpen = C.byId("drawer").className;
    C.currentDrawer = null;
    C.syncDrawerPanePolling();
    return settle();
  }).then(function () {
    out.wideAfterDrawerClose = {
      drawerClasses: C.byId("drawer").className,
      stored: window.localStorage.getItem("centrale-drawer-wide"),
      hasPane: !!C.byId("drawer-pane-pre")
    };
    // ...and the next drawer with a live pane opens wide again, off the
    // remembered preference alone.
    board();
    C.syncDrawerPanePolling();
    return settle();
  }).then(function () {
    out.wideOnReopen = {
      drawerClasses: C.byId("drawer").className,
      label: C.byId("drawer-pane-wide-toggle").textContent
    };
    C.byId("drawer-pane-wide-toggle").click();   // back to narrow
  });
}

pollLifecycle()
  .then(drawerRendersLeaveThePaneAlone)
  .then(theaterMovesTheOnePane)
  .then(escapeLadder)
  .then(cadence)
  .then(burstEndsWithTheTheaterAnd403DropsTheTier)
  .then(staleCaptureBlocksTheSend)
  .then(sessionKeysGoAsKeysNotText)
  .then(gates)
  .then(wideMode)
  .then(function () { process.stdout.write(JSON.stringify(out)); })
  .catch(function (err) {
    process.stderr.write((err && err.stack) || String(err));
    process.exit(1);
  });
"""


@js_harness.requires_node
class LivePaneBehaviourTests(unittest.TestCase):
    """The live pane's single-poller contract, observed rather than grepped.

    Task-60 gave the drawer one pane poller; task-73 put a theater over
    it, task-93 a rail beside it and task-114 three cadences on it, and
    each of those restated "there is still only one poller" in source
    text from its own side -- a forbidden-substring list per new
    function. The claim they were all making is measurable: run the real
    sources, and count the timers and the requests.
    """

    @property
    def out(self):
        # Read, not set up: the driver runs once per process, on the
        # first test that needs it, so a driver that throws fails THAT
        # test with node's stderr rather than erroring the whole class
        # out of a fixture -- the failure mode task-108 exists to fix.
        return js_harness.cached_driver(self, PANE_DRIVER_JS)

    # -- the poll itself --

    def test_the_poll_fetches_only_the_open_drawers_task(self):
        # Two live sessions in the project, one open drawer: a poller
        # that walked sessionsData would fetch twice per tick.
        start = self.out["afterStart"]
        self.assertEqual(
            start["requests"],
            ["GET /api/session-pane?project=my-tool&task=TASK-77&lines=200"])
        self.assertEqual(
            self.out["afterTwoTicks"]["requests"],
            ["GET /api/session-pane?project=my-tool&task=TASK-77&lines=200"] * 3)

    def test_exactly_one_timer_is_armed_at_a_time(self):
        # The whole single-poller contract, as a number. Two ticks later
        # it is still one, so nothing accumulated.
        self.assertEqual(self.out["afterStart"]["armed"], [2000])
        self.assertEqual(self.out["afterTwoTicks"]["armed"], [2000])

    def test_a_pane_tick_never_re_renders_the_board(self):
        # task-50: a 2s pane tick that rebuilt the board would reset
        # every lane's scroll position under the reader.
        self.assertEqual(self.out["rendersDuringPolling"], {"board": 0, "sessions": 0})

    def test_the_capture_lands_in_the_panes_own_element(self):
        start = self.out["afterStart"]
        self.assertEqual(start["text"], "one\ntwo\nthree")
        self.assertEqual(
            start["area"],
            ["drawer-pane-header", "drawer-pane-pre", "drawer-pane-error", "drawer-pane-reply"])

    def test_a_drawer_render_never_disturbs_the_pane_section(self):
        # The other direction of task-50: only the poller writes
        # #drawer-pane-area, so re-rendering the drawer's session, spawn
        # and harvest areas leaves the capture, the reply row and the
        # armed tick exactly as they were.
        after = self.out["afterDrawerRenders"]
        self.assertIs(after["unchanged"], True)
        self.assertEqual(after["text"], "one\ntwo\nthree")
        self.assertEqual(after["armed"], [2000])
        self.assertEqual(after["requests"], 3)   # no render fetched anything

    def test_a_decorative_run_is_clamped_and_real_content_is_not(self):
        # A 60-character box-drawing rule is collapsed to 40 so it fits
        # the drawer; 60 x's is content and overflows into the <pre>'s
        # own scroll untouched.
        self.assertEqual(self.out["clamped"], [40, 60])

    # -- the theater --

    def test_maximize_moves_the_one_pane_node_and_gives_it_back(self):
        # Not a copy: through both transitions there is exactly one of
        # each pane element in the whole document, and it is the same
        # object the drawer had.
        opened = self.out["theaterOpen"]
        self.assertEqual(opened["areaParent"], "theater")
        self.assertIs(opened["sameNode"], True)
        self.assertEqual(opened["paneNodes"], 1)
        self.assertEqual(opened["areaNodes"], 1)
        self.assertEqual(opened["ariaHidden"], "false")
        self.assertIn("open", opened["theaterClasses"])
        closed = self.out["theaterClosed"]
        self.assertEqual(closed["areaParent"], "drawer")
        self.assertEqual(closed["paneNodes"], 1)
        self.assertNotIn("open", closed["theaterClasses"])

    def test_opening_the_theater_fetches_nothing_and_arms_no_second_timer(self):
        # It re-renders the full window from the payload the poller
        # already has; a second surface with a poll of its own would be
        # a request and a timer here.
        self.assertEqual(self.out["theaterOpen"]["newRequests"], 0)
        self.assertEqual(self.out["theaterOpen"]["armed"], [2000])
        self.assertEqual(self.out["theaterClosed"]["newRequests"], 0)
        self.assertEqual(self.out["theaterClosed"]["armed"], [2000])

    def test_the_control_says_which_way_it_goes(self):
        self.assertEqual(self.out["theaterOpen"]["label"], "Close")
        self.assertEqual(self.out["theaterClosed"]["label"], "Maximize")

    def test_the_task_rail_comes_and_goes_with_the_theater(self):
        self.assertEqual(self.out["theaterOpen"]["railNodes"], 1)
        self.assertEqual(self.out["theaterClosed"]["railNodes"], 0)
        # task-93's rail renders from memory: it is inside the window
        # that fetched nothing (see the test above).

    def test_maximize_opens_on_the_newest_output_and_close_restores_the_drawer(self):
        # task-114: the drawer's 40-line tail and the theater's 200-line
        # window are different documents, so a pixel offset carried
        # between them points nowhere. Maximize goes to the bottom; close
        # puts the drawer back at the offset it was actually at.
        self.assertEqual(self.out["theaterScroll"], {"onOpen": 5000, "onClose": 120})

    # -- the Escape ladder --

    def test_escape_closes_one_layer_at_a_time_outermost_first(self):
        settings = self.out["escapeWithSettingsOpen"]
        self.assertEqual(settings["settingsClosed"], 1)
        self.assertIs(settings["theaterOpen"], True)    # untouched underneath
        self.assertIs(settings["drawerOpen"], True)
        theater = self.out["escapeClosesTheater"]
        self.assertIs(theater["theaterOpen"], False)
        self.assertIs(theater["drawerOpen"], True)      # not two layers at once
        self.assertEqual(theater["armed"], [2000])      # and the poll runs on
        self.assertEqual(theater["areaParent"], "drawer")

    def test_closing_the_drawer_stops_the_poll_and_empties_the_pane(self):
        drawer = self.out["escapeClosesDrawer"]
        self.assertIs(drawer["drawerOpen"], False)
        self.assertEqual(drawer["armed"], [])           # nothing left ticking
        self.assertEqual(drawer["area"], [])
        self.assertEqual(drawer["areaParent"], "drawer")  # sent home before emptying
        self.assertNotIn("open", drawer["drawerClasses"])

    # -- the adaptive cadence (task-114) --

    def test_the_cadence_changes_the_one_timers_delay_and_never_adds_one(self):
        self.assertEqual(self.out["cadenceIdle"], [2000])       # nobody acting
        self.assertEqual(self.out["cadenceWorking"], [1000])    # the badge says working
        self.assertEqual(self.out["afterSend"]["armed"], [300])  # just after a reply
        self.assertEqual(self.out["cadenceAfterBurst"], [1000])  # the burst decayed

    def test_a_reply_posts_once_and_pulls_the_next_capture_forward(self):
        send = self.out["afterSend"]
        self.assertEqual(send["body"],
                         {"project": "my-tool", "taskId": "TASK-77", "text": "yes"})
        # The POST, then the immediate re-capture -- and nothing else.
        self.assertEqual(
            [r for r in send["requests"] if r.startswith("POST")],
            ["POST /api/session-input"])
        self.assertEqual(send["requests"][-1],
                         "GET /api/session-pane?project=my-tool&task=TASK-77&lines=200")
        self.assertEqual(send["status"], 'sent "yes"')

    def test_leaving_the_theater_ends_the_burst_at_once(self):
        self.assertEqual(self.out["burstInTheater"], [300])
        self.assertEqual(self.out["burstAfterLeavingTheater"], [1000])

    def test_a_refused_reply_drops_to_the_read_only_tier_not_to_off(self):
        # A 403 mid-session means Settings turned replying off; the pane
        # itself is still allowed, so the row goes and the poll stays.
        after = self.out["after403"]
        self.assertEqual(after["mode"], "view")
        self.assertIs(after["hasReplyRow"], False)
        self.assertIs(after["hasPane"], True)
        self.assertEqual(after["armed"], [1000])
        self.assertEqual(after["toast"],
                         "Replying from the drawer is disabled in Settings.")

    # -- the staleness gate (task-61) --

    def test_a_stale_capture_blocks_the_send_before_it_leaves_the_page(self):
        gate = self.out["staleGate"]
        self.assertEqual(gate["status"], "sending blocked: capture is 15s old")
        self.assertIs(gate["sendDisabled"], True)
        self.assertEqual(self.out["staleSend"]["posts"], [])
        self.assertEqual(self.out["staleSend"]["status"],
                         "sending blocked: capture is 15s old")

    # -- the two session keys (task-135) --

    def test_the_reply_row_offers_exactly_esc_and_enter_as_keys(self):
        self.assertEqual(
            [b["label"] for b in self.out["keyButtons"]], ["Esc", "Enter"])
        self.assertEqual(
            [b["key"] for b in self.out["keyButtons"]], ["Escape", "Enter"])
        # Each says what it does to the pane, and neither promises more.
        for button in self.out["keyButtons"]:
            self.assertIn("in the session", button["title"])

    def test_a_key_click_posts_a_key_body_and_never_a_text_one(self):
        # The whole point: a pane parked on a startup modal cannot be
        # answered with text -- the line lands where there is no prompt and
        # its trailing Enter confirms the focused option. So a key click
        # must carry "key" and no "text" at all.
        esc = self.out["afterEsc"]
        self.assertEqual(esc["body"],
                         {"project": "my-tool", "taskId": "TASK-77", "key": "Escape"})
        self.assertNotIn("text", esc["body"])
        self.assertEqual(esc["posts"], ["POST /api/session-input"])
        self.assertEqual(esc["status"], "sent Esc")
        self.assertEqual(esc["armed"], [300])   # same burst a text reply gets
        self.assertEqual(self.out["afterEnterKey"]["body"],
                         {"project": "my-tool", "taskId": "TASK-77", "key": "Enter"})
        self.assertEqual(self.out["afterEnterKey"]["status"], "sent Enter")

    def test_a_key_click_leaves_the_typed_draft_alone_and_then_sends_it(self):
        # The user typed a reply the modal was blocking; dismissing the
        # modal must not throw that draft away.
        self.assertEqual(self.out["afterEsc"]["draft"], "it looks good")
        self.assertEqual(self.out["afterEnterKey"]["draft"], "it looks good")
        queued = self.out["afterQueuedReply"]
        self.assertEqual(queued["body"],
                         {"project": "my-tool", "taskId": "TASK-77", "text": "it looks good"})
        self.assertEqual(queued["status"], 'sent "it looks good"')
        self.assertEqual(queued["draft"], "")   # a text send clears it, as before

    def test_a_key_is_refused_by_the_same_stale_gate_as_text(self):
        self.assertEqual(self.out["staleKey"]["newRequests"], 0)
        self.assertEqual(self.out["staleKey"]["status"],
                         "sending blocked: capture is 15s old")

    # -- the two gates on starting at all --

    def test_the_poll_never_starts_with_the_feature_off_or_no_live_session(self):
        for case in ("tierOff", "noLiveSession"):
            with self.subTest(case):
                self.assertEqual(self.out[case]["requests"], [])
                self.assertEqual(self.out[case]["armed"], [])
                self.assertEqual(self.out[case]["area"], [])

    def test_the_view_tier_polls_but_renders_no_reply_row(self):
        view = self.out["viewTier"]
        self.assertEqual(
            view["requests"],
            ["GET /api/session-pane?project=my-tool&task=TASK-77&lines=200"])
        self.assertEqual(view["armed"], [2000])
        self.assertIs(view["hasPane"], True)
        self.assertIs(view["hasReplyRow"], False)


# ---------------------------------------------------------------------
# task-68: the drawer's wide mode
# ---------------------------------------------------------------------

# The preference is read once, when state.js loads, so a driver that
# wants to see what a hostile store does has to install it BEFORE the
# sources -- which is why this is its own driver rather than another
# phase of the one above. `process.argv[2]` is what the store holds:
# "1", some other string, "absent" for an empty store, or "throwing"
# for a browser that refuses site data outright (a private window, or
# storage turned off).
WIDE_STORAGE_DRIVER_JS = js_harness.LOAD_SOURCES_JS + r"""
var MODE = process.argv[2];
if (MODE === "throwing") {
  window.localStorage = {
    getItem: function () { throw new Error("storage is disabled"); },
    setItem: function () { throw new Error("storage is disabled"); },
    removeItem: function () { throw new Error("storage is disabled"); }
  };
} else if (MODE !== "absent") {
  window.localStorage.setItem("centrale-drawer-wide", MODE);
}

var C = loadFrontend(["state.js", "dom.js", "tasks.js", "board.js",
                      "spawn.js", "harvest.js", "drawer.js", "pane.js"]);
C.renderBoard = function () {};
C.renderSessionsPanel = function () {};
C.doRefresh = function () {};
C.fetchSessions = function () {};
C.showToast = function () {};
global.fetch = function () {
  return Promise.resolve({
    ok: true, status: 200,
    json: function () {
      return Promise.resolve({ lines: ["one"], capturedAt: "2026-09-04T10:00:00Z" });
    }
  });
};
wireDrawerShell();

var task = {
  id: "TASK-77", title: "A task", status: "In Progress", ready: true,
  hasSpawnBranch: true, alreadyMerged: false, worktreeDirty: false,
  branchCheckout: { kind: "centrale" }
};
C.knownProjects.length = 0;
C.knownProjects.push("my-tool");
C.boardData = { projects: [{ name: "my-tool", tasks: [task], statuses: ["To Do", "In Progress", "Done"] }],
                capabilities: { tmux: true } };
C.sessionsData = [{ name: "centrale-my-tool-TASK-77", project: "my-tool", agentState: "waiting" }];
C.currentDrawer = { project: "my-tool", id: "TASK-77", summary: task };
C.sessionPreviewMode = "interact";
C.syncDrawerPanePolling();

var out = {};
settle().then(function () {
  out.onOpen = {
    drawerClasses: C.byId("drawer").className,
    label: C.byId("drawer-pane-wide-toggle").textContent,
    pressed: C.byId("drawer-pane-wide-toggle").getAttribute("aria-pressed")
  };
  // A store that refuses reads refuses writes too: the toggle still
  // has to work, for this page view at least.
  C.byId("drawer-pane-wide-toggle").click();
  out.afterToggle = {
    drawerClasses: C.byId("drawer").className,
    label: C.byId("drawer-pane-wide-toggle").textContent,
    hasPane: !!C.byId("drawer-pane-pre")
  };
  process.stdout.write(JSON.stringify(out));
  // This driver runs the pane's REAL timer (the width has nothing to do
  // with the cadence), so close the drawer to clear it -- node exits
  // when the loop drains, and a poll that re-arms itself never drains.
  C.currentDrawer = null;
  C.syncDrawerPanePolling();
}).catch(function (err) {
  process.stderr.write((err && err.stack) || String(err));
  process.exit(1);
});
"""


@js_harness.requires_node
class DrawerWideModeBehaviourTests(unittest.TestCase):
    """Task-68's wide mode, as the width the drawer actually has.

    The source-text version of this family read the toggle's id out of
    the pane skeleton, the storage key out of state.js and a
    forbidden-call list out of `applyDrawerWidth` -- three greps for
    "clicking Expand widens the drawer and disturbs nothing else",
    which is one observation. It rides the same driver as the live pane
    above, so "disturbs nothing else" is measured against the same
    request log, board-render counter and armed-timer list that prove
    the single-poller contract.

    What stays textual, in test_server.py's DrawerWideModeContractTests,
    is the CSS -- this shim has no layout engine, so `max-width: 90vw`,
    the fixed drawer and the below-880px override are unobservable here
    -- and the two counts that say the toggle is built in exactly one
    place and the key named in exactly two.
    """

    @property
    def out(self):
        return js_harness.cached_driver(self, PANE_DRIVER_JS)

    def test_the_toggle_sits_in_the_pane_section_header_next_to_maximize(self):
        # It exists only where the pane section does, which is what makes
        # "wide" a property of a drawer that has a live pane.
        home = self.out["wideToggleHome"]
        self.assertIs(home["inPaneHeader"], True)
        self.assertEqual(home["headerButtons"],
                         ["drawer-pane-wide-toggle", "drawer-pane-theater-toggle"])

    def test_a_fresh_drawer_opens_at_the_summary_width_and_says_expand(self):
        before = self.out["wideBefore"]
        self.assertEqual(before["label"], "Expand")
        self.assertEqual(before["pressed"], "false")
        self.assertNotIn("wide", before["drawerClasses"])
        self.assertIsNone(before["stored"])

    def test_expand_widens_the_drawer_and_touches_nothing_else(self):
        # The whole of task-68's "deliberately touches nothing else", as
        # numbers: no fetch, no board or sessions render, the one pane
        # timer still armed at its idle cadence, the capture still on
        # screen -- and the reader's scroll position in the <pre> exactly
        # where they left it.
        on = self.out["wideOn"]
        self.assertIn("wide", on["drawerClasses"])
        self.assertEqual(on["label"], "Narrow")
        self.assertEqual(on["pressed"], "true")
        self.assertEqual(on["title"], "Back to the summary width")
        self.assertEqual(on["stored"], "1")
        self.assertEqual(on["newRequests"], 0)
        self.assertEqual(on["boardRenders"], 0)
        self.assertEqual(on["sessionRenders"], 0)
        self.assertEqual(on["armed"], [2000])
        self.assertEqual(on["paneScroll"], 77)
        self.assertEqual(on["text"], "one\ntwo\nthree")

    def test_narrow_puts_it_back_and_remembers_that_too(self):
        off = self.out["wideOff"]
        self.assertNotIn("wide", off["drawerClasses"])
        self.assertEqual(off["label"], "Expand")
        self.assertEqual(off["pressed"], "false")
        self.assertEqual(off["stored"], "0")

    def test_wide_lasts_exactly_as_long_as_the_pane_section(self):
        # The preference outlives the drawer; the width does not. A
        # drawer with no live pane has no toggle to un-widen it, so a
        # remembered "wide" must not follow it there.
        self.assertIn("wide", self.out["wideWhileOpen"])
        closed = self.out["wideAfterDrawerClose"]
        self.assertNotIn("wide", closed["drawerClasses"])
        self.assertIs(closed["hasPane"], False)
        self.assertEqual(closed["stored"], "1")
        reopened = self.out["wideOnReopen"]
        self.assertIn("wide", reopened["drawerClasses"])
        self.assertEqual(reopened["label"], "Narrow")

    # -- the store, including one that refuses --

    def _stored(self, mode):
        return js_harness.cached_driver(self, WIDE_STORAGE_DRIVER_JS, mode)

    def test_a_remembered_choice_is_applied_the_moment_the_pane_appears(self):
        self.assertIn("wide", self._stored("1")["onOpen"]["drawerClasses"])
        self.assertEqual(self._stored("1")["onOpen"]["label"], "Narrow")

    def test_anything_but_the_stored_yes_opens_at_the_summary_width(self):
        for mode in ("0", "absent", "true"):
            with self.subTest(mode):
                self.assertNotIn("wide", self._stored(mode)["onOpen"]["drawerClasses"])

    def test_a_store_that_refuses_costs_the_page_nothing_but_the_memory(self):
        # A private window, or site data turned off: reading throws, so
        # the width defaults to the summary one -- and writing throws, so
        # the choice lasts only this page view. Neither may take the
        # drawer, the toggle or the pane down with it.
        refused = self._stored("throwing")
        self.assertNotIn("wide", refused["onOpen"]["drawerClasses"])
        self.assertEqual(refused["onOpen"]["label"], "Expand")
        after = refused["afterToggle"]
        self.assertIn("wide", after["drawerClasses"])
        self.assertEqual(after["label"], "Narrow")
        self.assertIs(after["hasPane"], True)



# ---------------------------------------------------------------------
# task-70 / task-80 / task-81: a branch checked out somewhere else
# ---------------------------------------------------------------------

# Renders the board and the drawer for one task per branch state and
# reads back what a viewer would see: the badge on the card, the buttons
# in the drawer's three areas, whether each one is disabled, what its
# tooltip says, and -- the part a grep cannot reach -- what clicking it
# actually sends. `Date.now` is frozen so the "last commit N ago" half of
# every external reason is a constant rather than a race.
#
# One board, one renderBoard(), eight cards: precedence between the
# badges is then a fact about a single render rather than about the order
# of two `if`s in a file. Task ids are numeric because a live session's
# name has to parse (see parseSessionTask) -- SCENARIOS maps each one
# back to the branch state it stands for.
BRANCH_STATE_DRIVER_JS = js_harness.LOAD_SOURCES_JS + r"""
var C = loadFrontend(["state.js", "dom.js", "tasks.js", "board.js",
                      "spawn.js", "harvest.js", "drawer.js"]);

// The files this driver does not load, as inert sinks.
C.renderSessionsPanel = function () {};
C.syncDrawerPanePolling = function () {};
C.renderProjectChips = function () {};
C.renderErrorBanners = function () {};
C.doRefresh = function () {};
C.fetchSessions = function () {};
C.showToast = function () {};
C.copyText = function () {};
// main.js boots with a doRefresh() -- stubbed just above -- and carries
// renderAll, which is what task-81's "the drawer follows the board's
// next refetch" claim runs through. Loaded after the sinks, not with
// the rest.
loadFrontend(["main.js"]);

// 2026-09-04T12:00:00Z, with every branch tip two hours behind it.
var NOW = 1788523200000;
Date.now = function () { return NOW; };
var LAST_COMMIT = "2026-09-04T10:00:00Z";

var requests = [];
global.fetch = function (url, opts) {
  requests.push(((opts && opts.method) || "GET") + " " + url);
  return Promise.resolve({
    ok: true, status: 200,
    json: function () { return Promise.resolve({ merged: true, branch: "task/X" }); }
  });
};

var EXTERNAL = { kind: "external", path: "/repos/other-worktree", lastCommitAt: LAST_COMMIT };
var CENTRALE = { kind: "centrale", path: "/repos/.centrale-worktrees/x" };
var PARKED = { kind: "none" };

// name -> the branch state that name means. The name is what the
// assertions read; the id is what the session-name parser needs.
var SCENARIOS = [
  { name: "centrale", opts: {} },
  { name: "dirty", opts: { dirty: true } },
  // Dirty AND external: the two badges the card has to choose between.
  { name: "external", opts: { checkout: EXTERNAL, dirty: true } },
  { name: "parked", opts: { checkout: PARKED } },
  { name: "noKind", opts: { checkout: null } },
  { name: "mergedExternal", opts: { checkout: EXTERNAL, alreadyMerged: true } },
  { name: "merged", opts: { alreadyMerged: true } },
  { name: "liveExternal", opts: { checkout: EXTERNAL, dirty: true, live: "working" } },
  { name: "badge", opts: {} }
];

function build(name) {
  var i = 0;
  for (; i < SCENARIOS.length; i++) if (SCENARIOS[i].name === name) break;
  var opts = SCENARIOS[i].opts;
  return {
    id: "TASK-" + (i + 1), title: "A task", status: "In Progress", ready: true,
    hasSpawnBranch: "hasSpawnBranch" in opts ? opts.hasSpawnBranch : true,
    alreadyMerged: !!opts.alreadyMerged,
    worktreeDirty: !!opts.dirty,
    branchCheckout: "checkout" in opts ? opts.checkout : CENTRALE,
    agentState: opts.agentState
  };
}
var TASKS = SCENARIOS.map(function (s) { return build(s.name); });
function idOf(name) {
  return TASKS[SCENARIOS.map(function (s) { return s.name; }).indexOf(name)].id;
}
function taskNamed(name) {
  return C.findTask("my-tool", idOf(name));
}
var PROJECT = { name: "my-tool", tasks: TASKS.slice(), statuses: ["To Do", "In Progress", "Done"] };

function resetBoard() {
  C.knownProjects.length = 0;
  C.knownProjects.push("my-tool");
  C.activeProjects = new Set(["my-tool"]);
  PROJECT.tasks = TASKS.slice();
  C.boardData = { projects: [PROJECT], capabilities: { tmux: true } };
  C.sessionsData = SCENARIOS.filter(function (s) { return s.opts.live; }).map(function (s) {
    return { name: "centrale-my-tool-" + idOf(s.name).toLowerCase(),
             project: "my-tool", agentState: s.opts.live };
  });
  C.firstLoadDone = true;
  [C.harvestStates, C.spawnStates, C.cleanupStates, C.endSessionStates,
   C.spawnConfirmPending, C.cleanupConfirmPending, C.adoptDoneConfirmPending,
   C.discardMainConfirmPending].forEach(function (map) {
    Object.keys(map).forEach(function (k) { delete map[k]; });
  });
  requests.length = 0;
}

function byClass(node, className) {
  var hit = node.childNodes.filter(function (n) {
    return String(n.className || "").split(/\s+/).indexOf(className) !== -1;
  });
  return hit[0] || null;
}
function chips(row) {
  return row ? row.childNodes.map(function (n) {
    return { className: n.className, text: n.textContent, title: n.title || "" };
  }) : null;
}
function buttons(node) {
  return node.querySelectorAll("button").map(function (b) {
    return { text: b.textContent, disabled: !!b.disabled, title: b.title || "" };
  });
}
function allCards() {
  var found = [];
  (function walk(n) {
    if (String(n.className || "").split(/\s+/).indexOf("card") !== -1) { found.push(n); return; }
    n.childNodes.forEach(walk);
  })(C.byId("board"));
  return found;
}
function cardNamed(name) {
  var id = idOf(name);
  return allCards().filter(function (c) { return byClass(c, "card-id").textContent === id; })[0];
}
function cardsByScenario() {
  var map = {};
  SCENARIOS.forEach(function (s) {
    var card = cardNamed(s.name);
    map[s.name] = card && {
      badges: chips(byClass(card, "card-meta")),
      buttons: buttons(card)
    };
  });
  return map;
}
function area(id) {
  var el = C.byId(id);
  return {
    buttons: buttons(el),
    lines: el.childNodes.filter(function (n) {
      return String(n.className || "").indexOf("spawn-status-line") === 0;
    }).map(function (n) { return n.textContent; }),
    children: el.childNodes.map(function (n) { return n.tagName + "|" + n.className; })
  };
}
function openOn(name, opts) {
  opts = opts || {};
  var id = idOf(name);
  var key = C.spawnKey("my-tool", id);
  delete C.harvestStates[key];
  if (opts.harvestState) C.harvestStates[key] = opts.harvestState;
  C.currentDrawer = { project: "my-tool", id: id, summary: taskNamed(name) };
  if ("branchTask" in opts) C.currentDrawer.branchTask = opts.branchTask;
  C.renderDrawerSpawnArea();
  C.renderDrawerHarvestArea();
  C.renderDrawerSessionArea();
  return { spawn: area("drawer-spawn-area"), harvest: area("drawer-harvest-area"),
           session: area("drawer-session-area") };
}

var out = {};

// -- the board, once --
resetBoard();
C.renderBoard();
out.cards = cardsByScenario();
out.reason = C.externalCheckoutReason(EXTERNAL);
out.mergeReason = C.externalMergeReason(EXTERNAL);

// -- what a click on each card's buttons actually sends --
out.clicks = {};
["external", "mergedExternal", "centrale", "merged"].forEach(function (name) {
  requests.length = 0;
  cardNamed(name).querySelectorAll("button").forEach(function (b) { b.click(); });
  out.clicks[name] = requests.slice();
});

// -- the drawer, per branch state --
resetBoard();
out.drawer = {
  external: openOn("external"),
  centrale: openOn("centrale"),
  mergedExternal: openOn("mergedExternal"),
  merged: openOn("merged")
};

// The drawer's Merge, clicked: the external one must reach nothing.
resetBoard();
openOn("external");
requests.length = 0;
C.byId("drawer-harvest-area").querySelectorAll("button").forEach(function (b) { b.click(); });
out.drawerExternalMergeClick = requests.slice();
resetBoard();
openOn("centrale");
requests.length = 0;
// The primary one only: task-119's two secondary actions share this
// area now, and what they send on a first click is their own tests'
// subject (they measure, they do not merge).
C.byId("drawer-harvest-area").querySelectorAll("button").forEach(function (b) {
  if (String(b.className).indexOf("btn-primary") !== -1) b.click();
});
out.drawerPlainMergeClick = requests.slice();

// -- which branch of the harvest area wins, by rendering the overlaps --
var DIVERGENCE = { branchStatus: "Done", lastBranchCommitSubject: "finish it" };
var DISCARDABLE = { path: "backlog/tasks/task-1.md" };
var BOTH = { doneDivergence: DIVERGENCE, discardableMainTaskEdit: DISCARDABLE };
resetBoard();
out.precedence = {
  // alreadyMerged outranks a divergence report...
  mergedOverDivergence: openOn("merged", { harvestState: { status: "blocked", report: BOTH } }).harvest.buttons,
  // ...divergence outranks a discardable board edit...
  divergenceOverDiscard: openOn("centrale", { harvestState: { status: "blocked", report: BOTH } }).harvest.buttons,
  // ...and both outrank the plain Merge.
  discardOverMerge: openOn("centrale", {
    harvestState: { status: "blocked", report: { discardableMainTaskEdit: DISCARDABLE } }
  }).harvest.buttons,
  plainMerge: openOn("centrale").harvest.buttons
};

// -- the agent badge's session scope (task-78) --
function badge(opts) {
  resetBoard();
  var id = idOf("badge");
  taskNamed("badge").agentState = opts.board;
  C.sessionsData = opts.live
    ? [{ name: "centrale-my-tool-" + id.toLowerCase(), project: "my-tool", agentState: opts.live }]
    : [];
  C.currentDrawer = { project: "my-tool", id: id, summary: taskNamed("badge"),
                      branchTask: opts.branchTask || null };
  C.renderDrawerSessionArea();
  var el = C.byId("drawer-session-area");
  return {
    children: el.childNodes.map(function (n) { return n.tagName + "|" + n.className; }),
    badge: el.firstChild ? { className: el.firstChild.className, text: el.firstChild.textContent } : null
  };
}
out.badges = {
  deadWorking: badge({ board: "working" }),
  deadWaiting: badge({ board: "waiting" }),
  deadIdle: badge({ board: "idle" }),
  deadFinished: badge({ board: "finished" }),
  liveWorking: badge({ board: "working", live: "working" }),
  liveWaiting: badge({ board: "waiting", live: "waiting" }),
  // The board's copy is the older one: a session that has since started
  // working must not show the previous session's "waiting".
  staleBoardResidue: badge({ board: "waiting", live: "working" }),
  hooklessDoneBranch: badge({ board: "unknown", live: "unknown",
                              branchTask: { status: "Done" } })
};

// -- task-81 AC #3: the drawer follows the board's next refetch --
resetBoard();
out.refetch = { before: openOn("external").harvest.buttons };
// The same task, refetched with the foreign worktree gone. renderAll is
// the whole refresh path; nothing else is touched.
PROJECT.tasks = PROJECT.tasks.map(function (t) {
  if (t.id !== idOf("external")) return t;
  var fresh = build("external");
  fresh.branchCheckout = CENTRALE;
  return fresh;
});
C.renderAll();
out.refetch.after = area("drawer-harvest-area").buttons;
out.refetch.summaryIsTheFreshTask =
  C.currentDrawer.summary === C.findTask("my-tool", idOf("external"));

require("fs").writeSync(1, JSON.stringify(out));
process.exit(0);
"""


@js_harness.requires_node
class ExternalBranchBehaviourTests(unittest.TestCase):
    """A branch checked out outside Centrale, as the page renders it.

    Tasks 70, 80 and 81 each stated the same thing in source text from
    their own side -- `'className: "badge-external"'` appearing before
    `'className: "badge-interrupted"'`, an `assertNotIn` over a sliced
    function body, the index of one `if` compared with the index of
    another. All three are claims about what a viewer sees and what a
    click can reach, so they are made here by rendering the board and
    the drawer and reading the buttons.

    `assertNotIn("addEventListener", body)` deserves its own note: it
    would pass for a button whose handler was attached one line outside
    the slice, and fail for one that listened for something harmless.
    What the tests below check instead is that clicking every button on
    the card and in the drawer sends nothing.

    What stays textual, in test_server.py's ExternalWorkContractTests
    and ExternalMergeDisabledContractTests, is the CSS the badge is
    (`.badge-external`), and the source-shape half of "derived from
    board data alone": that no name like `externalMergeDisabled` exists
    in any frontend file, and that the display rule's body never reads
    the in-flight harvest map. A run shows the rule agrees with the
    board it was given; only a read shows there is no second input.
    """

    @property
    def out(self):
        return js_harness.cached_driver(self, BRANCH_STATE_DRIVER_JS)

    def _badges(self, scenario):
        return [(b["className"], b["text"]) for b in self.out["cards"][scenario]["badges"]]

    # -- the card's badge --

    def test_worked_externally_outranks_interrupted_and_unmerged(self):
        # The "external" card is dirty AND has an unmerged branch, so all
        # three badges' conditions hold at once; exactly one is rendered.
        self.assertIn(("badge-external", "worked externally"), self._badges("external"))
        for className in ("badge-interrupted", "status-dot-branch"):
            self.assertNotIn(className, [b[0] for b in self._badges("external")])
        # ...and the badge carries the honest reason as its tooltip.
        external = [b for b in self.out["cards"]["external"]["badges"]
                    if b["className"] == "badge-external"][0]
        self.assertEqual(external["title"], self.out["reason"])

    def test_each_other_checkout_kind_gets_its_own_badge(self):
        self.assertIn(("badge-interrupted", "interrupted"), self._badges("dirty"))
        self.assertIn(("status-dot-branch", "parked branch"), self._badges("parked"))
        # A branch with no checkout record at all is a plain unmerged
        # one -- null is not "external" and not "parked".
        self.assertIn(("status-dot-branch", "unmerged branch"), self._badges("noKind"))
        self.assertIn(("status-dot-branch", "unmerged branch"), self._badges("centrale"))

    def test_a_live_session_drops_every_branch_badge_but_the_plain_one(self):
        # The same external checkout as above, plus a session: while an
        # agent is running, where the branch is checked out is not the
        # story, so the card falls all the way through to the plain
        # "there is a branch" badge.
        classNames = [b[0] for b in self._badges("liveExternal")]
        for className in ("badge-external", "badge-interrupted"):
            self.assertNotIn(className, classNames)
        self.assertIn(("status-dot-branch", "unmerged branch"), self._badges("liveExternal"))

    def test_the_reason_names_the_path_the_age_and_the_way_out(self):
        reason = self.out["reason"]
        self.assertIn("Worked externally at /repos/other-worktree", reason)
        self.assertIn("last commit 2 hours ago", reason)
        self.assertIn("Spawn, Resume and clean-up are disabled", reason)
        self.assertIn("git refuses a second checkout of the same branch", reason)
        merge_reason = self.out["mergeReason"]
        self.assertIn("Worked externally at /repos/other-worktree", merge_reason)
        self.assertIn("checked out outside Centrale", merge_reason)
        self.assertIn("merge becomes possible once that worktree is finished and removed",
                      merge_reason)

    # -- the card's footer --

    def test_the_cards_merge_is_disabled_and_silent_for_an_external_checkout(self):
        self.assertEqual(self.out["cards"]["external"]["buttons"],
                         [{"text": "Merge", "disabled": True, "title": self.out["mergeReason"]}])
        self.assertEqual(self.out["clicks"]["external"], [])

    def test_the_cards_merge_is_enabled_and_posts_for_every_other_kind(self):
        merge = self.out["cards"]["centrale"]["buttons"]
        self.assertEqual(len(merge), 1)
        self.assertEqual(merge[0]["text"], "Merge")
        self.assertIs(merge[0]["disabled"], False)
        self.assertEqual(
            merge[0]["title"],
            "Merge this task's branch back into the base branch, if all safety gates pass")
        self.assertEqual(self.out["clicks"]["centrale"], ["POST /api/harvest"])
        for other in ("parked", "noKind"):
            with self.subTest(other):
                self.assertIs(self.out["cards"][other]["buttons"][0]["disabled"], False)

    def test_an_out_of_band_merge_offers_cleanup_unless_the_checkout_is_foreign(self):
        # task-80: the clickable "Merged -- clean up" would only ever get
        # a 409 back while the branch is checked out elsewhere, so the
        # foreign case gets the disabled button and sends nothing.
        self.assertEqual(self.out["cards"]["merged"]["buttons"],
                         [{"text": "Merged — clean up", "disabled": False,
                           "title": "This branch is already fully merged -- remove its "
                                    "worktree and delete the branch."}])
        self.assertEqual(self.out["clicks"]["merged"], ["POST /api/cleanup-branch"])
        self.assertEqual(self.out["cards"]["mergedExternal"]["buttons"],
                         [{"text": "Worked externally", "disabled": True,
                           "title": self.out["reason"]}])
        self.assertEqual(self.out["clicks"]["mergedExternal"], [])

    # -- the drawer --

    def test_the_drawer_replaces_resume_and_respawn_with_the_disabled_action(self):
        # The external task is dirty, which is otherwise exactly the
        # state that offers Resume (task-116).
        spawn = self.out["drawer"]["external"]["spawn"]
        self.assertEqual(spawn["buttons"],
                         [{"text": "Worked externally", "disabled": True,
                           "title": self.out["reason"]}])
        self.assertEqual(spawn["lines"], [self.out["reason"]])
        # task-125: the branch that IS Centrale's own leaves this area
        # empty -- its Re-spawn is a member of the harvest area's one
        # secondary row now. The external case is the exception that
        # still renders here, because it replaces those actions with an
        # explanation rather than joining them.
        self.assertEqual(self.out["drawer"]["centrale"]["spawn"]["buttons"], [])
        self.assertEqual([b["text"] for b in self.out["drawer"]["centrale"]["harvest"]["buttons"]],
                         ["Merge", "Re-spawn agent",
                          "Abandon worktree, keep branch", "Discard attempt"])

    def test_the_drawers_merge_matches_the_cards_and_reaches_nothing(self):
        external = self.out["drawer"]["external"]["harvest"]["buttons"]
        self.assertEqual(external,
                         [{"text": "Merge", "disabled": True, "title": self.out["mergeReason"]}])
        self.assertEqual(self.out["drawerExternalMergeClick"], [])
        plain = self.out["drawer"]["centrale"]["harvest"]["buttons"]
        self.assertIs(plain[0]["disabled"], False)
        self.assertEqual(self.out["drawerPlainMergeClick"], ["POST /api/harvest"])

    def test_the_drawers_cleanup_follows_the_same_rule_as_the_cards(self):
        # task-125: Re-spawn trails the cleanup primary in the same
        # secondary row it trails every other primary in -- a merged
        # branch offers no way to throw the attempt away, which is the
        # claim the throwaway test below still makes.
        self.assertEqual(
            [b["text"] for b in self.out["drawer"]["merged"]["harvest"]["buttons"]],
            ["Merged — clean up", "Re-spawn agent"])
        self.assertEqual(self.out["drawer"]["mergedExternal"]["harvest"]["buttons"],
                         [{"text": "Worked externally", "disabled": True,
                           "title": self.out["reason"]}])

    def test_the_harvest_areas_four_actions_keep_their_order_of_precedence(self):
        # Each case below holds BOTH conditions at once, so which button
        # renders is the precedence, observed. Only the PRIMARY action is
        # at stake: task-119's two secondary throwaway buttons trail
        # every unmerged case and are asserted separately below.
        p = self.out["precedence"]

        def primary(buttons):
            # Everything the secondary row holds is filtered out here:
            # task-119's two throwaway actions and, since task-125 put
            # them in the same row, the spawn actions beside them.
            return [b["text"] for b in buttons
                    if b["text"] not in ("Abandon worktree, keep branch",
                                         "Discard attempt",
                                         "Resume agent", "Re-spawn agent")]

        self.assertEqual(primary(p["mergedOverDivergence"]), ["Merged — clean up"])
        self.assertEqual(primary(p["divergenceOverDiscard"]),
                         ["Adopt board Done onto branch & merge"])
        self.assertEqual(primary(p["discardOverMerge"]),
                         ["Discard redundant board edit & merge"])
        self.assertEqual(primary(p["plainMerge"]), ["Merge"])

    def test_the_throwaway_actions_trail_every_unmerged_primary_but_not_a_merged_one(self):
        # task-119: whichever primary action wins above, the two ways
        # out of a bad attempt come after it -- except on an already
        # merged branch, where "Merged -- clean up" is the right action
        # and the work is safely on the base already.
        p = self.out["precedence"]
        throwaway = ["Abandon worktree, keep branch", "Discard attempt"]
        for case in ("divergenceOverDiscard", "discardOverMerge", "plainMerge"):
            self.assertEqual([b["text"] for b in p[case]][-2:], throwaway, case)
        self.assertEqual([b["text"] for b in p["mergedOverDivergence"]],
                         ["Merged — clean up", "Re-spawn agent"])

    def test_the_drawer_re_enables_merge_on_the_next_refetch_with_no_reload(self):
        # task-81 AC #3: the open drawer's summary is re-pointed at the
        # freshly fetched board task, so the rule -- which reads nothing
        # but that task -- follows the board by itself.
        self.assertIs(self.out["refetch"]["before"][0]["disabled"], True)
        self.assertIs(self.out["refetch"]["after"][0]["disabled"], False)
        self.assertIs(self.out["refetch"]["summaryIsTheFreshTask"], True)


@js_harness.requires_node
class AgentBadgeSessionScopeBehaviourTests(unittest.TestCase):
    """Task-78: a badge that describes a SESSION, shown only while one lives.

    The server's event cache keeps the last event per (project, task),
    so "working" outlives the tmux session that reported it. The claim
    -- that a session-scoped state with no live session renders nothing
    at all -- is about what the drawer puts on screen, so it is read off
    the drawer's session area rather than out of the gate's source.

    What stays textual, in test_server.py's AgentBadgeSessionScopeTests,
    is the one claim about the gate rather than about a state: that it
    is the function's ONLY `return null`. A driven test can show that
    the states it enumerates are suppressed; only a read of the function
    can show that nothing else is.
    """

    @property
    def out(self):
        return js_harness.cached_driver(self, BRANCH_STATE_DRIVER_JS)

    def test_a_session_scoped_state_with_no_live_session_renders_nothing(self):
        for case in ("deadWorking", "deadWaiting", "deadIdle"):
            with self.subTest(case):
                self.assertEqual(self.out["badges"][case]["children"], [])

    def test_a_durable_state_survives_its_session(self):
        # "finished" is a fact about the task, not about the session that
        # reported it, so it is still shown once tmux is gone.
        self.assertEqual(self.out["badges"]["deadFinished"]["badge"],
                         {"className": "agent-badge agent-badge-finished", "text": "finished"})

    def test_the_same_states_render_unchanged_with_a_live_session(self):
        self.assertEqual(self.out["badges"]["liveWorking"]["badge"],
                         {"className": "agent-badge agent-badge-working", "text": "working"})
        waiting = self.out["badges"]["liveWaiting"]
        self.assertEqual(waiting["badge"],
                         {"className": "agent-badge agent-badge-waiting",
                          "text": "waiting for input"})
        # ...along with the attach hint and End session, which is what
        # makes a suppressed badge a visible loss rather than a cosmetic
        # one.
        self.assertEqual(waiting["children"],
                         ["span|agent-badge agent-badge-waiting",
                          "div|drawer-waiting-attach",
                          "button|btn btn-danger-outline"])

    def test_the_live_sessions_state_beats_the_boards_older_copy(self):
        # /api/sessions is fetched right after a spawn, where the board
        # summary can still be carrying the previous session's value.
        self.assertEqual(self.out["badges"]["staleBoardResidue"]["badge"]["text"], "working")

    def test_an_unknown_state_on_a_done_branch_reads_as_likely_finished(self):
        self.assertEqual(self.out["badges"]["hooklessDoneBranch"]["badge"],
                         {"className": "agent-badge agent-badge-likely-finished",
                          "text": "likely finished"})



# ---------------------------------------------------------------------
# task-86 / task-91: the milestone the board already knew about
# ---------------------------------------------------------------------

# Three projects that between them cover every milestone case task-86 and
# task-91 argue about: a milestone with a resolved title and one without,
# a title that repeats across two projects AND inside one, a project name
# and a milestone id that both need percent-encoding, three spellings of
# "no milestone", and a task whose only chip is a label. The dropdown,
# the chips, the lanes and the drawer are then all read off ONE board --
# so "the same milestone" meaning one thing in the filter and another in
# a chip would show up as a disagreement between two readings of the same
# render.
MILESTONE_DRIVER_JS = js_harness.LOAD_SOURCES_JS + r"""
var C = loadFrontend(["state.js", "dom.js", "tasks.js", "board.js",
                      "spawn.js", "harvest.js", "drawer.js"]);

C.renderSessionsPanel = function () {};
C.syncDrawerPanePolling = function () {};
C.renderProjectChips = function () {};
C.renderErrorBanners = function () {};
C.doRefresh = function () {};
C.fetchSessions = function () {};
C.showToast = function () {};
C.copyText = function () {};
// shell.js owns the dropdown's change handler and main.js owns
// renderAll's order, so both are loaded -- after the sinks above, since
// main.js boots with a doRefresh() and shell.js wires the header to it.
loadFrontend(["shell.js", "main.js"]);

var requests = [];
var TASK_VIEW = null;
global.fetch = function (url, opts) {
  requests.push(((opts && opts.method) || "GET") + " " + url);
  return Promise.resolve({
    ok: true, status: 200,
    json: function () { return Promise.resolve(TASK_VIEW); }
  });
};

// renderBoard, counted and timestamped: "the dropdown is repopulated
// before the lanes it filters" is a fact about the moment renderBoard
// runs, so it is read there.
var renderedBoards = 0;
var optionsWhenBoardRendered = null;
var realRenderBoard = C.renderBoard;
C.renderBoard = function () {
  renderedBoards++;
  optionsWhenBoardRendered = C.byId("milestone-filter").childNodes.length;
  realRenderBoard();
};

function task(id, opts) {
  opts = opts || {};
  var t = {
    id: id, title: "A task", status: opts.status || "In Progress", ready: true,
    hasSpawnBranch: false, alreadyMerged: false, worktreeDirty: false
  };
  if ("milestone" in opts) t.milestone = opts.milestone;
  if ("milestoneTitle" in opts) t.milestoneTitle = opts.milestoneTitle;
  if (opts.labels) t.labels = opts.labels;
  return t;
}

var STATUSES = ["To Do", "In Progress", "Done"];
function alpha() {
  return { name: "alpha", statuses: STATUSES, tasks: [
    task("TASK-1", { milestone: "m-0", milestoneTitle: "post-0.2.0" }),
    task("TASK-2", { milestone: "m-0", milestoneTitle: "post-0.2.0", status: "Done" }),
    // Two of alpha's own milestones share a title with each other and
    // with one of beta's.
    task("TASK-3", { milestone: "m-1", milestoneTitle: "shared name", status: "To Do" }),
    task("TASK-4", { milestone: "m-2", milestoneTitle: "shared name" }),
    task("TASK-5", { milestone: "m-2", milestoneTitle: "shared name", status: "Done" }),
    // Three spellings of "no milestone", and a task whose only chip is
    // a label.
    task("TASK-6", { labels: ["chore"] }),
    task("TASK-7"),
    task("TASK-8", { milestone: null }),
    task("TASK-9", { milestone: "   " }),
    // A milestone the server could not resolve a title for.
    task("TASK-99", { milestone: "m-9" })
  ] };
}
function beta() {
  return { name: "beta", statuses: STATUSES, tasks: [
    task("TASK-1", { milestone: "m-0", milestoneTitle: "shared name" }),
    task("TASK-2", { milestone: "m-0", milestoneTitle: "shared name", status: "Done" })
  ] };
}
// A project name and a milestone id that both need percent-encoding: a
// key built by plain concatenation would be ambiguous with them.
function slashed() {
  return { name: "a/b", statuses: STATUSES, tasks: [
    task("TASK-1", { milestone: "m/0", milestoneTitle: "encoded" })
  ] };
}

function setBoard(projects, active) {
  C.knownProjects.length = 0;
  projects.forEach(function (p) { C.knownProjects.push(p.name); });
  C.activeProjects = new Set(active || C.knownProjects);
  C.boardData = { projects: projects, capabilities: { tmux: true } };
  C.sessionsData = [];
  C.firstLoadDone = true;
  C.lastMilestoneFilterSignature = null;
  C.lastRenderedFetchPayload = null;
  C.currentDrawer = null;
  requests.length = 0;
}

function options() {
  return C.byId("milestone-filter").childNodes.map(function (o) {
    return { value: o.getAttribute("value"), text: o.textContent };
  });
}
function dropdown() {
  C.renderMilestoneFilter();
  return { options: options(), fieldHidden: !!C.byId("milestone-field").hidden,
           value: C.byId("milestone-filter").value };
}
function hasClass(node, className) {
  return String(node.className || "").split(/\s+/).indexOf(className) !== -1;
}
function childByClass(node, className) {
  return node.childNodes.filter(function (n) { return hasClass(n, className); })[0] || null;
}
function deepByClass(node, className) {
  if (hasClass(node, className)) return node;
  for (var i = 0; i < node.childNodes.length; i++) {
    var hit = deepByClass(node.childNodes[i], className);
    if (hit) return hit;
  }
  return null;
}
function cards() {
  var found = [];
  (function walk(n) {
    if (hasClass(n, "card")) { found.push(n); return; }
    n.childNodes.forEach(walk);
  })(C.byId("board"));
  return found;
}
function cardName(card) {
  return deepByClass(card, "proj-chip").textContent + "/" + childByClass(card, "card-id").textContent;
}
// Every rendered card -> the chips row under its meta line, or null when
// the card built no chips row at all.
function renderedChips() {
  var map = {};
  cards().forEach(function (card) {
    var row = childByClass(card, "card-labels");
    map[cardName(card)] = row ? row.childNodes.map(function (n) {
      return { className: n.className, text: n.textContent, title: n.title || "" };
    }) : null;
  });
  return map;
}
function lanes() {
  var map = {};
  C.byId("board").querySelectorAll(".column-body[data-status]").forEach(function (body) {
    map[body.getAttribute("data-status")] = cards().filter(function (c) {
      return c.parentNode === body;
    }).map(cardName).sort();
  });
  return map;
}

var out = {};

// -- the whole board, once --
setBoard([alpha(), beta(), slashed()]);
C.renderAll();
out.chips = renderedChips();
out.dropdown = { options: options(), fieldHidden: !!C.byId("milestone-field").hidden };
out.dropdownRequests = requests.slice();
out.optionsWhenBoardRendered = optionsWhenBoardRendered;

// The <select> is rebuilt only when its derived list or the selection
// changed: an open dropdown must survive a background refresh.
var firstOption = C.byId("milestone-filter").childNodes[0];
C.renderMilestoneFilter();
out.unchangedRefreshKeptTheOptionNodes =
  C.byId("milestone-filter").childNodes[0] === firstOption;
C.boardData.projects[0].tasks.push(task("TASK-100", { milestone: "m-3", milestoneTitle: "new one" }));
C.renderMilestoneFilter();
out.afterANewMilestoneAppeared = options().map(function (o) { return o.text; });
out.rebuiltTheOptionNodes = C.byId("milestone-filter").childNodes[0] !== firstOption;

// -- the filter gate, across every project and lane --
setBoard([alpha(), beta(), slashed()]);
C.milestoneFilter = "alpha/m-0";
C.renderBoard();
out.filteredToAlphaM0 = lanes();
C.milestoneFilter = "beta/m-0";
C.renderBoard();
out.filteredToBetaM0 = lanes();
C.milestoneFilter = "a%2Fb/m%2F0";
C.renderBoard();
out.filteredToSlashed = lanes();
C.milestoneFilter = "a/b/m/0";      // the same thing, spelled un-encoded
C.renderBoard();
out.filteredToUnencodedSlashed = lanes();
C.milestoneFilter = "";
C.renderBoard();
out.unfiltered = lanes();

// -- what the dropdown says --
setBoard([alpha(), beta(), slashed()]);
C.milestoneFilter = "";
out.allProjects = dropdown();
setBoard([alpha(), beta(), slashed()], ["alpha"]);
C.milestoneFilter = "";
out.alphaOnly = dropdown();
// The board's milestones all gone, with a selection still pointing at one.
var empty = alpha();
empty.tasks = empty.tasks.filter(function (t) { return !t.milestone; });
setBoard([empty], ["alpha"]);
C.milestoneFilter = "alpha/m-0";
out.vanishedSelection = dropdown();
C.milestoneFilter = "";
out.noMilestonesAtAll = dropdown();

// A milestone whose TITLE is "__proto__", in two projects: the labels
// have to qualify it, which means counting it in a map a title cannot
// reach the prototype of.
var pa = alpha(); pa.tasks = [task("TASK-1", { milestone: "m-0", milestoneTitle: "__proto__" })];
var pb = beta(); pb.tasks = [task("TASK-1", { milestone: "m-0", milestoneTitle: "__proto__" })];
setBoard([pa, pb]);
C.milestoneFilter = "";
out.protoTitles = dropdown().options.map(function (o) { return o.text; });

// -- a rename keeps the selection, because the key carries the id --
var renamed = alpha();
renamed.tasks.forEach(function (t) {
  if (t.milestone === "m-0") t.milestoneTitle = "post-0.3.0";
});
setBoard([renamed, beta(), slashed()]);
C.milestoneFilter = "alpha/m-0";
C.renderBoard();
out.afterRename = {
  dropdown: dropdown(),
  lanes: lanes(),
  chip: renderedChips()["alpha/TASK-1"]
};

// -- changing the selection: what it re-renders, and what it stores --
setBoard([alpha(), beta(), slashed()]);
C.milestoneFilter = "";
C.renderBoardAndSessionsIfChanged();       // seeds task-50's skip payload
var payloadBefore = C.lastRenderedFetchPayload;
var boardsBefore = renderedBoards;
requests.length = 0;
var select = C.byId("milestone-filter");
select.value = "beta/m-0";
select.dispatch("change", { target: select });
out.selectionChange = {
  filter: C.milestoneFilter,
  stored: window.localStorage.getItem("centrale-milestone-filter"),
  boardRenders: renderedBoards - boardsBefore,
  requests: requests.slice(),
  lanes: lanes()
};
C.renderBoardAndSessionsIfChanged();       // an unchanged refresh: must skip
out.selectionChange.rendersAfterUnchangedRefresh = renderedBoards - boardsBefore;
out.selectionChange.skipPayloadUnchanged = C.lastRenderedFetchPayload === payloadBefore;

// -- the drawer's own chip, resolved from the board rather than fetched --
setBoard([alpha(), beta(), slashed()]);
C.milestoneFilter = "";
function sections() {
  return C.byId("drawer-body").childNodes
    .filter(function (n) { return n.attrs && n.attrs["data-section"]; })
    .map(function (s) {
      return {
        key: s.attrs["data-section"],
        className: s.className,
        header: s.childNodes[0].textContent,
        chips: s.childNodes[1].childNodes.map(function (row) {
          return { className: row.className,
                   chips: row.childNodes.map(function (c) {
                     return { className: c.className, text: c.textContent };
                   }) };
        })
      };
    });
}
function openOn(project, id) {
  var p = C.boardData.projects.filter(function (x) { return x.name === project; })[0];
  var summary = p.tasks.filter(function (t) { return t.id === id; })[0];
  // /api/task is a passthrough of `task view --json`: it carries the
  // milestone ID and no title at all.
  var view = { id: id, title: "A task", status: summary.status,
               description: "short", acceptanceCriteria: [], dependencies: [] };
  if (summary.milestone) view.milestone = summary.milestone;
  TASK_VIEW = { task: view, branchTask: null };
  requests.length = 0;
  C.openDrawer(p, summary);
  return settle().then(function () {
    return { sections: sections(), requests: requests.slice() };
  });
}

openOn("alpha", "TASK-1").then(function (r) {
  out.drawerWithMilestone = r;
  return openOn("alpha", "TASK-99");
}).then(function (r) {
  out.drawerUnresolvedTitle = r;
  return openOn("alpha", "TASK-7");
}).then(function (r) {
  out.drawerWithoutMilestone = r;
  require("fs").writeSync(1, JSON.stringify(out));
  process.exit(0);
}).catch(function (err) {
  process.stderr.write((err && err.stack) || String(err));
  process.exit(1);
});
"""


# The selection is restored when state.js loads, so what the store holds
# has to be installed before the sources. `process.argv[2]` is the raw
# stored value, or "absent" for an empty store.
MILESTONE_STORAGE_DRIVER_JS = js_harness.LOAD_SOURCES_JS + r"""
var STORED = process.argv[2];
if (STORED !== "absent") window.localStorage.setItem("centrale-milestone-filter", STORED);
var C = loadFrontend(["state.js"]);
process.stdout.write(JSON.stringify({ filter: C.milestoneFilter }));
"""


@js_harness.requires_node
class MilestoneSurfacingBehaviourTests(unittest.TestCase):
    """Task-86: the milestone /api/board already passes through, surfaced.

    A chip on the card, a row in the drawer, a dropdown in the filter
    row, and one more gate on the shared filter path -- all of it
    derived from the loaded board on every render, with no endpoint and
    no milestone state of Centrale's own. Every claim in that sentence
    is about what one render produces from one board, so it is made
    here against exactly that: one board, one renderAll, and the chips,
    options and lanes read back off it.

    What stays textual, in test_server.py's
    MilestoneSurfacingContractTests, is what the render is not: the CSS
    that tells a milestone chip from a label chip, the shell markup that
    hosts an empty <select> (a hardcoded <option> there would be a
    second source of truth this shim would happily render), the lane
    scroll capture -- whose restore clamps against a freshly built
    element's height, which a shim with no layout cannot give it -- and
    the greps over every frontend file and server.py that say no
    milestone endpoint or write exists anywhere.
    """

    @property
    def out(self):
        return js_harness.cached_driver(self, MILESTONE_DRIVER_JS)

    # -- the card --

    def test_the_milestone_chip_leads_the_chips_row(self):
        # A task with a milestone and a label gets both, milestone first.
        self.assertEqual(self.out["chips"]["alpha/TASK-1"],
                         [{"className": "milestone-chip", "text": "post-0.2.0",
                           "title": "Milestone: post-0.2.0"}])
        self.assertEqual(self.out["chips"]["alpha/TASK-6"],
                         [{"className": "label-chip", "text": "chore", "title": ""}])

    def test_a_task_with_neither_a_milestone_nor_labels_builds_no_chips_row(self):
        # The row is built for "a milestone OR labels", so a task with
        # neither renders exactly the DOM it did before the feature.
        for task_id in ("alpha/TASK-7", "alpha/TASK-8", "alpha/TASK-9"):
            with self.subTest(task_id):
                self.assertIsNone(self.out["chips"][task_id])

    def test_a_missing_null_and_blank_milestone_are_the_same_absence(self):
        # TASK-7 has no milestone key at all, TASK-8 has null, TASK-9 has
        # whitespace -- and none of the three is in the dropdown either.
        values = [o["value"] for o in self.out["dropdown"]["options"]]
        self.assertEqual([v for v in values if v.startswith("alpha/")],
                         ["alpha/m-9", "alpha/m-0", "alpha/m-1", "alpha/m-2"])

    # -- the drawer --

    def test_the_drawer_gets_a_milestone_section_only_when_the_task_has_one(self):
        section = self.out["drawerWithMilestone"]["sections"][0]
        self.assertEqual(section["key"], "milestone")
        self.assertEqual(section["header"], "Milestone")
        self.assertIn("drawer-milestone-section", section["className"])
        self.assertEqual(section["chips"],
                         [{"className": "drawer-milestone-row",
                           "chips": [{"className": "milestone-chip", "text": "post-0.2.0"}]}])
        self.assertNotIn("milestone",
                         [s["key"] for s in self.out["drawerWithoutMilestone"]["sections"]])

    # -- the dropdown --

    def test_the_dropdown_is_built_from_the_board_and_fetches_nothing(self):
        # No endpoint and no cached list: the options are exactly the
        # milestones on the board that was already loaded.
        self.assertEqual(self.out["dropdownRequests"], [])
        self.assertEqual([o["text"] for o in self.out["dropdown"]["options"]],
                         ["All milestones",
                          "encoded · 0/1 done",
                          "m-9 · 0/1 done",
                          "post-0.2.0 · 1/2 done",
                          "shared name · alpha · 0/1 done",
                          "shared name · alpha · 1/2 done",
                          "shared name · beta · 1/2 done"])

    def test_the_default_option_is_all_milestones_with_an_empty_value(self):
        first = self.out["dropdown"]["options"][0]
        self.assertEqual(first, {"value": "", "text": "All milestones"})

    def test_the_dropdown_follows_the_project_selection(self):
        # beta and a/b toggled off: their milestones go with them, and so
        # does the qualifier on the title they were making ambiguous...
        self.assertEqual([o["text"] for o in self.out["alphaOnly"]["options"]],
                         ["All milestones",
                          "m-9 · 0/1 done",
                          "post-0.2.0 · 1/2 done",
                          "shared name · alpha · 0/1 done",
                          "shared name · alpha · 1/2 done"])

    def test_the_counts_are_of_the_milestone_not_of_what_is_on_screen(self):
        # "post-0.2.0 · 1/2 done" while the board shows every lane: one
        # of its two tasks is Done. The other filters never touch it.
        labels = {o["value"]: o["text"] for o in self.out["dropdown"]["options"]}
        self.assertEqual(labels["alpha/m-0"], "post-0.2.0 · 1/2 done")
        self.assertEqual(labels["beta/m-0"], "shared name · beta · 1/2 done")

    def test_a_selection_that_vanished_from_the_board_stays_visible_at_zero(self):
        # Its tasks completed into backlog/completed/, or its project
        # toggled off: the entry stays, showing 0/0, so an empty board
        # has a visible reason instead of a selection reset behind the
        # user's back. Its title went with its tasks, so it reads as the
        # id it has always been.
        self.assertEqual([o["text"] for o in self.out["vanishedSelection"]["options"]],
                         ["All milestones", "m-0 · 0/0 done"])
        self.assertEqual(self.out["vanishedSelection"]["value"], "alpha/m-0")
        self.assertIs(self.out["vanishedSelection"]["fieldHidden"], False)

    def test_a_board_with_no_milestones_hides_the_whole_field(self):
        self.assertIs(self.out["noMilestonesAtAll"]["fieldHidden"], True)
        self.assertEqual([o["text"] for o in self.out["noMilestonesAtAll"]["options"]],
                         ["All milestones"])

    def test_the_dropdown_is_repopulated_before_the_lanes_it_filters(self):
        # Six milestones plus "All milestones": renderAll rebuilt the
        # options before it rebuilt the board, so a milestone that
        # appeared is selectable in the same pass that renders its tasks.
        self.assertEqual(self.out["optionsWhenBoardRendered"], 7)

    def test_an_unchanged_refresh_leaves_an_open_dropdown_alone(self):
        # Rebuilding a <select> closes it under whoever has it open, and
        # this runs on every changed refresh -- so it rebuilds only when
        # the derived list or the selection actually moved.
        self.assertIs(self.out["unchangedRefreshKeptTheOptionNodes"], True)
        self.assertIs(self.out["rebuiltTheOptionNodes"], True)
        self.assertIn("new one · 0/1 done", self.out["afterANewMilestoneAppeared"])

    # -- the filter --

    def test_every_project_and_every_lane_narrows_through_the_one_gate(self):
        # There is no per-column or per-project milestone code: selecting
        # alpha's m-0 leaves exactly its two tasks, in the two lanes they
        # are in, and empties every other project.
        self.assertEqual(self.out["filteredToAlphaM0"],
                         {"To Do": [], "In Progress": ["alpha/TASK-1"], "Done": ["alpha/TASK-2"]})
        self.assertEqual(self.out["filteredToBetaM0"],
                         {"To Do": [], "In Progress": ["beta/TASK-1"], "Done": ["beta/TASK-2"]})
        # ...and clearing it brings all twelve back.
        restored = self.out["unfiltered"]
        self.assertEqual(sum(len(v) for v in restored.values()), 13)

    def test_changing_the_selection_re_filters_directly_and_remembers_it(self):
        change = self.out["selectionChange"]
        self.assertEqual(change["filter"], "beta/m-0")
        self.assertEqual(change["stored"], "beta/m-0")
        self.assertEqual(change["requests"], [])          # nothing is refetched
        self.assertEqual(change["boardRenders"], 1)       # rendered once, directly
        self.assertEqual(change["lanes"],
                         {"To Do": [], "In Progress": ["beta/TASK-1"], "Done": ["beta/TASK-2"]})

    def test_the_selection_is_view_state_and_never_enters_the_skip_payload(self):
        # task-50 skips a refresh whose FETCHED state is unchanged. The
        # milestone selection is not fetched state, so a refresh right
        # after a filter change still skips -- which is only safe because
        # the change already re-rendered directly (above).
        change = self.out["selectionChange"]
        self.assertIs(change["skipPayloadUnchanged"], True)
        self.assertEqual(change["rendersAfterUnchangedRefresh"], 1)


@js_harness.requires_node
class MilestoneIdentityBehaviourTests(unittest.TestCase):
    """Task-91: the title is what is shown, the id is what is meant.

    Backlog assigns milestone ids per repo and sequentially, so every
    repo's first milestone is "m-0"; a task carries the id, and the
    server resolves the title alongside it. That makes identity
    project + id and display the title -- and the whole family is about
    those two never being confused. Driven, because both halves are
    observable at once: the option labels and the chips say the title,
    the option VALUES and the filter say the key, and a rename moves one
    without moving the other.

    What stays textual, in test_server.py's
    MilestoneIdentityContractTests, is the pair of source-shape claims a
    render cannot make: that the id-only helpers are not published on
    the seam at all, so no renderer can reach one by accident, and that
    `task.milestoneTitle` is read in exactly one place across every
    frontend file. Plus one prototype-safety half: the option counter's
    map is reachable by a user-supplied title, and is driven below; the
    entry map's is not, because its keys always carry a "/" -- the
    `Object.create(null)` there is what keeps that true if the key shape
    ever changes, and only a read of the source can say it is still
    there.
    """

    @property
    def out(self):
        return js_harness.cached_driver(self, MILESTONE_DRIVER_JS)

    def _labels(self):
        return {o["value"]: o["text"] for o in self.out["dropdown"]["options"]}

    # -- the title is display --

    def test_both_chips_show_the_title_and_fall_back_to_the_id(self):
        self.assertEqual(self.out["chips"]["alpha/TASK-1"][0]["text"], "post-0.2.0")
        # A project with no milestone file, or a `backlog milestone list`
        # this build cannot parse, leaves milestoneTitle unset -- the chip
        # then reads the id rather than going blank.
        self.assertEqual(self.out["chips"]["alpha/TASK-99"][0]["text"], "m-9")
        self.assertEqual(self._labels()["alpha/m-9"], "m-9 · 0/1 done")

    def test_the_drawers_chip_resolves_its_title_from_the_board_it_already_has(self):
        # /api/task is a passthrough of `task view --json`: it carries
        # the id and no title. Without the board-side lookup this chip
        # would read "m-0" while the card behind it read "post-0.2.0" --
        # and the lookup costs no request of its own.
        drawer = self.out["drawerWithMilestone"]
        self.assertEqual(drawer["sections"][0]["chips"][0]["chips"][0]["text"], "post-0.2.0")
        self.assertEqual(drawer["requests"], ["GET /api/task?project=alpha&id=TASK-1"])
        # ...and an id the board cannot resolve either still shows the id.
        unresolved = self.out["drawerUnresolvedTitle"]
        self.assertEqual(unresolved["sections"][0]["chips"][0]["chips"][0]["text"], "m-9")

    # -- the id is identity --

    def test_two_projects_first_milestones_are_two_entries_not_one(self):
        # alpha/m-0 and beta/m-0 are different milestones with the same
        # id. A dropdown keyed on the bare id would fold them into one
        # entry with a merged 2/4 count.
        labels = self._labels()
        self.assertEqual(labels["alpha/m-0"], "post-0.2.0 · 1/2 done")
        self.assertEqual(labels["beta/m-0"], "shared name · beta · 1/2 done")

    def test_the_key_survives_a_project_name_and_an_id_that_contain_slashes(self):
        # "a/b" + "m/0" concatenated raw would parse back as project "a";
        # both halves are percent-encoded around the one separator, so
        # the value round-trips and the filter still selects that task.
        self.assertEqual(self._labels()["a%2Fb/m%2F0"], "encoded · 0/1 done")
        self.assertEqual(self.out["filteredToSlashed"]["In Progress"], ["a/b/TASK-1"])
        # The un-encoded spelling is a different key, and matches nothing.
        self.assertEqual(sum(len(v) for v in self.out["filteredToUnencodedSlashed"].values()), 0)

    def test_a_rename_moves_the_label_and_leaves_the_selection_where_it_was(self):
        # The key's payload is the id, so renaming a milestone in backlog
        # renames it everywhere it is shown without invalidating a
        # selection made under the old name.
        after = self.out["afterRename"]
        self.assertEqual(after["dropdown"]["value"], "alpha/m-0")
        labels = {o["value"]: o["text"] for o in after["dropdown"]["options"]}
        self.assertEqual(labels["alpha/m-0"], "post-0.3.0 · 1/2 done")
        self.assertEqual(after["chip"][0]["text"], "post-0.3.0")
        self.assertEqual(after["lanes"],
                         {"To Do": [], "In Progress": ["alpha/TASK-1"], "Done": ["alpha/TASK-2"]})

    def test_the_project_name_qualifies_an_entry_only_where_the_title_repeats(self):
        # "post-0.2.0" is alpha's alone and stays short; "shared name" is
        # carried by two of alpha's milestones and one of beta's, so all
        # three say which project they are in.
        labels = self._labels()
        self.assertEqual(labels["alpha/m-0"], "post-0.2.0 · 1/2 done")
        for key in ("alpha/m-1", "alpha/m-2", "beta/m-0"):
            with self.subTest(key):
                self.assertIn(" · alpha · " if key.startswith("alpha") else " · beta · ",
                              labels[key])

    def test_entries_sort_by_title_then_project_then_id(self):
        self.assertEqual([o["value"] for o in self.out["dropdown"]["options"]],
                         ["", "a%2Fb/m%2F0", "alpha/m-9", "alpha/m-0",
                          "alpha/m-1", "alpha/m-2", "beta/m-0"])

    def test_a_title_that_names_a_prototype_property_is_still_counted(self):
        # The option counter is keyed by titles straight off the board.
        # On a plain object, `counts["__proto__"]` reads Object.prototype
        # and the increment is silently dropped -- so two milestones by
        # that name would stop qualifying each other and become
        # indistinguishable in the dropdown.
        self.assertEqual(self.out["protoTitles"],
                         ["All milestones",
                          "__proto__ · alpha · 0/1 done",
                          "__proto__ · beta · 0/1 done"])

    # -- what a pre-task-91 store holds --

    def _restored(self, stored):
        return js_harness.cached_driver(self, MILESTONE_STORAGE_DRIVER_JS, stored)["filter"]

    def test_a_bare_id_left_by_an_older_build_is_dropped_not_restored(self):
        # Before task-91 the stored selection was "m-0". Restoring that
        # would filter on a key no entry has, emptying the board with no
        # visible cause; a key is recognised by its separator.
        self.assertEqual(self._restored("m-0"), "")
        self.assertEqual(self._restored("alpha/m-0"), "alpha/m-0")
        self.assertEqual(self._restored("absent"), "")



# ---------------------------------------------------------------------
# task-94: a merge verdict that was remembered too long
# ---------------------------------------------------------------------

# The driver: loads the real static/ sources over that shim and walks the
# exact flow TASK-94 reported, clicking the real rendered buttons.
STALE_VERDICT_DRIVER_JS = r"""
var fs = require("fs");
var path = require("path");
var vm = require("vm");

var STATIC = process.argv[1];
["state.js", "dom.js", "tasks.js", "board.js", "spawn.js", "harvest.js", "drawer.js"].forEach(function (name) {
  vm.runInThisContext(fs.readFileSync(path.join(STATIC, name), "utf8"), { filename: name });
});
var C = window.Centrale;

// Stubs for the three files this harness does not load (api.js,
// feedback.js, sessions.js). Each is an inert sink, so EVERY request the
// flow makes goes through the recorder below and nowhere else -- which
// is what makes the recorded request list meaningful.
var requests = [];
C.renderBoard = function () {};
C.renderSessionsPanel = function () {};
C.doRefresh = function () {};
C.fetchSessions = function () {};
C.showToast = function () {};
C.openDrawer = function () {};

var RESPONSES = {};
global.fetch = function (url, opts) {
  requests.push(url + " " + opts.method);
  var data = RESPONSES[url];
  return Promise.resolve({ ok: true, status: 200, json: function () { return Promise.resolve(data); } });
};

var SESSION = "centrale-my-tool-TASK-77";
function board() {
  var task = {
    id: "TASK-77", title: "A task", status: "In Progress", ready: true,
    hasSpawnBranch: true, alreadyMerged: false, worktreeDirty: false,
    branchCheckout: { kind: "centrale" }
  };
  C.knownProjects.length = 0;
  C.knownProjects.push("my-tool");
  C.boardData = { projects: [{ name: "my-tool", tasks: [task] }], capabilities: { tmux: true } };
  C.sessionsData = [];
  C.currentDrawer = { project: "my-tool", id: "TASK-77", summary: task, branchTask: null };
  [C.harvestStates, C.spawnStates, C.endSessionStates].forEach(function (map) {
    Object.keys(map).forEach(function (k) { delete map[k]; });
  });
  requests.length = 0;
}

// task-125: the area's secondary actions sit in one row element, so
// the row is expanded in place -- what this reads is still one entry
// per control, in the order the drawer renders them.
function lines(id) {
  var out = [];
  document.getElementById(id).childNodes.forEach(function (n) {
    var nodes = n.className === "drawer-action-row" ? n.childNodes : [n];
    nodes.forEach(function (m) {
      out.push(m.tagName + "|" + m.className + "|" + m.textContent);
    });
  });
  return out;
}
function button(text) {
  var hit = document.getElementById("drawer-harvest-area").querySelectorAll("button")
    .filter(function (n) { return n.textContent === text; });
  if (hit.length !== 1) throw new Error("expected 1 " + JSON.stringify(text) + " button, got " + hit.length);
  return hit[0];
}
function tick() { return new Promise(function (r) { setTimeout(r, 0); }); }

var BLOCKED = {
  merged: false, taskId: "TASK-77", branch: "task/TASK-77", baseBranch: "main",
  gates: [{ name: "mergeClean", passed: false, reason: "merge conflict: static/app.js" }],
  behindBase: { baseBranch: "main", count: 3 },
  reconcileHint: "this branch is 3 commits behind main"
};
var MERGED = { merged: true, taskId: "TASK-77", branch: "task/TASK-77", baseBranch: "main", gates: [] };
RESPONSES["/api/resume"] = { session: SESSION, attach: "tmux attach", agent: "claude" };
RESPONSES["/api/end-session"] = { ended: true };

var out = {};

// Scenario A -- the reported flow, every step through the real button:
// Merge (blocked at the mergeClean gate) -> Resume to reconcile (arm,
// then confirm) -> End session.
function reportedFlow() {
  board();
  RESPONSES["/api/harvest"] = BLOCKED;
  C.renderDrawerHarvestArea();
  button("Merge").click();
  return tick().then(function () {
    C.renderDrawerHarvestArea();
    out.afterBlockedMerge = lines("drawer-harvest-area");
    button("Resume to reconcile").click();
    C.renderDrawerHarvestArea();
    button("Confirm resume to reconcile?").click();
    return tick();
  }).then(function () {
    C.sessionsData = [{ name: SESSION, project: "my-tool", agentState: "working" }];
    C.renderDrawerHarvestArea();
    out.duringResumedSession = lines("drawer-harvest-area");
    C.renderEndSessionButton("my-tool", "TASK-77", "btn").click();
    return tick();
  }).then(function () {
    C.sessionsData = [];  // the session is gone: the suppression lifts
    C.renderDrawerHarvestArea();
    out.afterEndSession = lines("drawer-harvest-area");
    out.requests = requests.slice();
    out.remembered = Object.keys(C.harvestStates);
  });
}

// Scenario B -- a SUCCESSFUL merge verdict must survive both paths.
function successSurvives() {
  board();
  RESPONSES["/api/harvest"] = MERGED;
  C.renderDrawerHarvestArea();
  button("Merge").click();
  return tick().then(function () {
    C.resumeTask("my-tool", "TASK-77");
    return tick();
  }).then(function () {
    C.renderEndSessionButton("my-tool", "TASK-77", "btn").click();
    return tick();
  }).then(function () {
    C.renderDrawerHarvestArea();
    C.renderDrawerSpawnArea();
    out.successHarvestArea = lines("drawer-harvest-area");
    out.successSpawnArea = lines("drawer-spawn-area");
    out.successStatus = (C.harvestStates["my-tool::TASK-77"] || {}).status || null;
  });
}

reportedFlow().then(successSurvives).then(function () {
  process.stdout.write(JSON.stringify(out));
});
"""


class StaleMergeVerdictBehaviourTests(unittest.TestCase):
    """Task-94's reported flow, walked over the real sources.

    Merge (blocked) -> Resume to reconcile -> End session brought the
    same red gate message back, describing a conflict the agent had
    already resolved. That is a claim about a sequence of clicks, so it
    is driven: the buttons the drawer actually rendered are clicked and
    the lines it actually printed are read back. The invalidation RULE
    and its two call sites stay source-text, in test_server.py's
    StaleMergeVerdictInvalidationTests -- "there is no second, unguarded
    deleter anywhere" is a claim about the shape of the source, and no
    walk of one flow can make it.
    """

    # -- AC #5: the reported flow, driven end to end --

    @js_harness.requires_node
    def test_the_reported_flow_leaves_no_stale_gate_message(self):
        out = js_harness.run_driver(self, STALE_VERDICT_DRIVER_JS)

        blocked_line = ("div|spawn-status-line error|Not ready: merge conflict: static/app.js"
                        " This branch is 3 commits behind main.")
        # 1. Merge comes back blocked: the gate message and task-66's
        #    reconcile offer are both there.
        # task-119's two throwaway actions sit last in the area: a
        # blocked merge is exactly the moment someone reaches for them.
        # task-125: the secondary actions are one row under the primary
        # -- the reconcile offer leads it, Re-spawn follows, the two
        # ways out of the attempt come last.
        throwaway = [
            "button|btn btn-sm btn-quiet|Abandon worktree, keep branch",
            "button|btn btn-sm btn-quiet|Discard attempt",
        ]
        self.assertEqual(out["afterBlockedMerge"], [
            "button|btn btn-primary|Merge",
            blocked_line,
            "button|btn btn-sm|Resume to reconcile",
            "button|btn btn-sm|Re-spawn agent",
        ] + throwaway)
        # 2-3. The resumed session takes the actions away -- this is
        #    what made the message look like it came BACK. task-120:
        #    they are now taken away VISIBLY (disabled, with the live
        #    session named) rather than by emptying the area, but the
        #    claim this step makes is unchanged: the stale gate line is
        #    not among what is rendered.
        self.assertNotIn(blocked_line, out["duringResumedSession"])
        self.assertEqual(
            ["|".join(entry.split("|")[:2]) for entry in out["duringResumedSession"]],
            ["button|btn btn-primary",
             "button|btn btn-sm btn-quiet",
             "button|btn btn-sm btn-quiet",
             "div|spawn-status-line"])
        self.assertIn("centrale-my-tool-TASK-77", out["duringResumedSession"][-1])
        # 4. After End session the suppression lifts -- and the stale
        #    verdict is gone, leaving the honest plain Merge button.
        # The resume's own "Spawned ..." line trails the row: same line
        # the spawn area printed before task-125 merged the two areas
        # into one, unchanged and still not a gate verdict.
        self.assertEqual(out["afterEndSession"],
                         ["button|btn btn-primary|Merge",
                          "button|btn btn-sm|Re-spawn agent"] + throwaway +
                         ["div|spawn-status-line success|Spawned (claude):"
                          " centrale-my-tool-TASK-77tmux attachCopy"])
        self.assertEqual(out["remembered"], [])
        # AC #4, observed rather than grepped: one POST per click, and
        # no /api/harvest at all after the first one.
        self.assertEqual(out["requests"], [
            "/api/harvest POST", "/api/resume POST", "/api/end-session POST"])

        # AC #3: the same two paths leave a SUCCESS verdict alone -- the
        # confirmation line still renders and the Spawn button is still
        # suppressed (task-41), exactly as before this change.
        self.assertEqual(out["successStatus"], "success")
        self.assertEqual(out["successHarvestArea"],
                         ["div|spawn-status-line success|Merged task/TASK-77 into main"])
        self.assertEqual(out["successSpawnArea"], [])


# ---------------------------------------------------------------------
# task-119: throwing a bad attempt away
# ---------------------------------------------------------------------

THROWAWAY_DRIVER_JS = js_harness.LOAD_SOURCES_JS + r"""
var C = loadFrontend(["state.js", "dom.js", "tasks.js", "board.js",
                      "spawn.js", "harvest.js", "drawer.js"]);

// The files this driver does not load, as inert sinks -- same shape as
// the task-81 driver above. main.js comes after them, for renderAll:
// "the drawer follows the board's next refetch" is part of the claim
// here too, since a discard has to survive the refetch it triggers.
C.renderSessionsPanel = function () {};
C.syncDrawerPanePolling = function () {};
C.renderProjectChips = function () {};
C.renderErrorBanners = function () {};
C.fetchSessions = function () {};
C.showToast = function () {};
C.copyText = function () {};
// Before main.js: it boots with a doRefresh() of its own.
C.doRefresh = function () {};
loadFrontend(["main.js"]);

var NOW = 1788523200000;
Date.now = function () { return NOW; };

// What GET /api/discard-preview answers, per test. Everything about the
// confirming label comes from here, which is the point: the numbers in
// the sentence are the server's measurement, not the board's guess.
var PREVIEW = {
  branch: "task/task-9",
  branchExists: true,
  branchTip: "1fb0261627d96454a5d478560631e4bbdc9bd015",
  baseBranch: "main",
  commitCount: 3,
  worktreePath: "/w/my-tool-task-9",
  worktreeExists: true,
  dirtyPaths: ["a.txt", "b.txt", "c.txt", "d.txt"],
  dirtyFileCount: 4,
  liveSession: null,
  externalCheckout: null,
  recoveryCommand: "git branch task/task-9 1fb0261627d96454a5d478560631e4bbdc9bd015"
};
var DISCARD_RESULT = {
  ok: true, branch: "task/task-9", branchDeleted: true, worktreeRemoved: true,
  branchTip: "1fb0261627d96454a5d478560631e4bbdc9bd015", baseBranch: "main",
  commitCount: 3, discardedPaths: ["a.txt", "b.txt", "c.txt", "d.txt"],
  recoveryTag: "abandoned/task-9-20260904-163012",
  recoveryCommand: "git branch task/task-9 1fb0261627d96454a5d478560631e4bbdc9bd015"
};
var ABANDON_RESULT = {
  ok: true, branch: "task/task-9", branchKept: true, worktreeRemoved: true,
  branchTip: "1fb0261627d96454a5d478560631e4bbdc9bd015", baseBranch: "main",
  commitCount: 3, discardedPaths: ["a.txt", "b.txt", "c.txt", "d.txt"]
};
var BASE_PREVIEW = JSON.parse(JSON.stringify(PREVIEW));
var PREVIEW_FAILS = null;   // set to an error string to make the GET 4xx
var POST_FAILS = null;      // set to an error string to make the POST 409

var requests = [];
var postBodies = [];        // task-121: what each destructive click actually sent
global.fetch = function (url, opts) {
  var method = (opts && opts.method) || "GET";
  requests.push(method + " " + url.split("?")[0]);
  if (opts && opts.body) {
    postBodies.push({ url: url.split("?")[0], body: JSON.parse(opts.body) });
  }
  if (url.indexOf("/api/discard-preview") === 0) {
    if (PREVIEW_FAILS) {
      return Promise.resolve({ ok: false, status: 409,
        json: function () { return Promise.resolve({ error: PREVIEW_FAILS }); } });
    }
    return Promise.resolve({ ok: true, status: 200,
      json: function () { return Promise.resolve(PREVIEW); } });
  }
  if (POST_FAILS) {
    return Promise.resolve({ ok: false, status: 409,
      json: function () { return Promise.resolve({ error: POST_FAILS }); } });
  }
  var body = url.indexOf("/api/abandon-worktree") === 0 ? ABANDON_RESULT : DISCARD_RESULT;
  return Promise.resolve({ ok: true, status: 200,
    json: function () { return Promise.resolve(body); } });
};

var CENTRALE = { kind: "centrale", path: "/w/my-tool-task-9" };
var EXTERNAL = { kind: "external", path: "/repos/other", lastCommitAt: "2026-09-04T10:00:00Z" };
var PARKED = { kind: "none", path: null };

var SCENARIOS = [
  { name: "bad", checkout: CENTRALE, dirty: true },
  { name: "parked", checkout: PARKED },
  { name: "external", checkout: EXTERNAL },
  { name: "merged", checkout: CENTRALE, alreadyMerged: true },
  { name: "live", checkout: CENTRALE, live: true },
  { name: "nobranch", checkout: null, hasSpawnBranch: false }
];
function build(s, i) {
  return {
    id: "TASK-" + (i + 1), title: "A task", status: "In Progress", ready: true,
    hasSpawnBranch: "hasSpawnBranch" in s ? s.hasSpawnBranch : true,
    alreadyMerged: !!s.alreadyMerged,
    worktreeDirty: !!s.dirty,
    branchCheckout: s.checkout
  };
}
var TASKS = SCENARIOS.map(build);
function idOf(name) {
  return TASKS[SCENARIOS.map(function (s) { return s.name; }).indexOf(name)].id;
}
var PROJECT = { name: "my-tool", tasks: TASKS.slice(), statuses: ["To Do", "In Progress", "Done"] };

function reset() {
  C.knownProjects.length = 0;
  C.knownProjects.push("my-tool");
  C.activeProjects = new Set(["my-tool"]);
  PROJECT.tasks = SCENARIOS.map(build);
  C.boardData = { projects: [PROJECT], capabilities: { tmux: true } };
  C.sessionsData = SCENARIOS.filter(function (s) { return s.live; }).map(function (s) {
    return { name: "centrale-my-tool-" + idOf(s.name).toLowerCase(), project: "my-tool",
             agentState: "working" };
  });
  C.firstLoadDone = true;
  [C.harvestStates, C.spawnStates, C.cleanupStates, C.endSessionStates, C.discardStates,
   C.abandonStates, C.spawnConfirmPending, C.cleanupConfirmPending, C.discardConfirmPending,
   C.abandonConfirmPending].forEach(function (map) {
    Object.keys(map).forEach(function (k) { delete map[k]; });
  });
  requests.length = 0;
  postBodies.length = 0;
  PREVIEW_FAILS = null;
  POST_FAILS = null;
}

function buttons(id) {
  return C.byId(id).querySelectorAll("button").map(function (b) {
    return { text: b.textContent, className: b.className, disabled: !!b.disabled,
             title: b.title || "" };
  });
}
function texts(id) { return buttons(id).map(function (b) { return b.text; }); }
function lines(id) {
  return C.byId(id).childNodes
    .filter(function (n) { return String(n.className || "").indexOf("spawn-status-line") === 0; })
    .map(function (n) { return n.textContent; });
}
function codes(id) {
  var found = [];
  (function walk(n) {
    if (n.tagName === "code") found.push(n.textContent);
    n.childNodes.forEach(walk);
  })(C.byId(id));
  return found;
}
function openOn(name) {
  var id = idOf(name);
  C.currentDrawer = { project: "my-tool", id: id,
                      summary: C.findTask("my-tool", id), branchTask: null };
  C.renderDrawerHarvestArea();
  C.renderDrawerSpawnArea();
  return id;
}
function clickNamed(id, text) {
  var hit = C.byId(id).querySelectorAll("button").filter(function (b) {
    return b.textContent === text;
  });
  if (hit.length !== 1) throw new Error("expected 1 button named " + text + ", got " + hit.length);
  hit[0].click();
}
function rerender() { C.renderDrawerHarvestArea(); C.renderDrawerSpawnArea(); }

var out = {};

// -- which branch states get the two actions at all --
reset();
out.offered = {};
out.offeredButtons = {};
out.offeredLines = {};
["bad", "parked", "external", "merged", "live", "nobranch"].forEach(function (name) {
  openOn(name);
  out.offered[name] = texts("drawer-harvest-area");
  // task-120: a live session renders the same actions DISABLED, so the
  // texts alone no longer say what a viewer can do -- read the disabled
  // flag and the tooltip too.
  out.offeredButtons[name] = buttons("drawer-harvest-area");
  out.offeredLines[name] = lines("drawer-harvest-area");
});

// -- the discard, end to end --
reset();
openOn("bad");
out.discard = { before: texts("drawer-harvest-area"),
                spawnBefore: texts("drawer-spawn-area") };
clickNamed("drawer-harvest-area", "Discard attempt");
out.discard.whileMeasuring = texts("drawer-harvest-area");
var chain = settle().then(function () {
  rerender();
  out.discard.armed = buttons("drawer-harvest-area");
  out.discard.armedLines = lines("drawer-harvest-area");
  out.discard.requestsAfterFirstClick = requests.slice();
  // The second click is the one that destroys.
  clickNamed("drawer-harvest-area", out.discard.armed[out.discard.armed.length - 1].text);
  return settle();
}).then(function () {
  rerender();
  out.discard.requests = requests.slice();
  out.discard.posted = postBodies.slice();
  out.discard.afterLines = lines("drawer-harvest-area");
  out.discard.afterCodes = codes("drawer-harvest-area");
  out.discard.afterButtons = texts("drawer-harvest-area");
  // AC #3: the ordinary Spawn button is back, in the same render pass.
  out.discard.spawnAfter = texts("drawer-spawn-area");
  // ...and it survives the board refetch that removes the branch.
  PROJECT.tasks = PROJECT.tasks.map(function (t) {
    if (t.id !== idOf("bad")) return t;
    var fresh = build(SCENARIOS[0], 0);
    fresh.hasSpawnBranch = false;
    fresh.branchCheckout = null;
    fresh.worktreeDirty = false;
    return fresh;
  });
  C.renderAll();
  out.discard.spawnAfterRefetch = texts("drawer-spawn-area");
  out.discard.harvestAfterRefetch = lines("drawer-harvest-area");
  out.discard.codesAfterRefetch = codes("drawer-harvest-area");
});

// -- the abandon, end to end --
chain = chain.then(function () {
  reset();
  openOn("bad");
  clickNamed("drawer-harvest-area", "Abandon worktree, keep branch");
  return settle();
}).then(function () {
  rerender();
  out.abandon = { armed: texts("drawer-harvest-area"),
                  armedLines: lines("drawer-harvest-area") };
  clickNamed("drawer-harvest-area", out.abandon.armed[out.abandon.armed.length - 2]);
  return settle();
}).then(function () {
  rerender();
  out.abandon.requests = requests.slice();
  out.abandon.posted = postBodies.slice();
  out.abandon.afterLines = lines("drawer-harvest-area");
  out.abandon.afterButtons = texts("drawer-harvest-area");
  // The branch survives, so the branch-bearing actions do too.
  out.abandon.spawnAfter = texts("drawer-spawn-area");
  // ...including after the refetch that reports the branch as parked.
  PROJECT.tasks = PROJECT.tasks.map(function (t) {
    if (t.id !== idOf("bad")) return t;
    var fresh = build(SCENARIOS[0], 0);
    fresh.branchCheckout = PARKED;
    fresh.worktreeDirty = false;
    return fresh;
  });
  C.renderAll();
  out.abandon.buttonsAfterRefetch = texts("drawer-harvest-area");
});

// -- the two refusals, as the arming click meets them --
chain = chain.then(function () {
  reset();
  PREVIEW_FAILS = "a live tmux session is still running: centrale-my-tool-task-1";
  openOn("bad");
  clickNamed("drawer-harvest-area", "Discard attempt");
  return settle();
}).then(function () {
  rerender();
  out.liveRefusal = { lines: lines("drawer-harvest-area"),
                      buttons: texts("drawer-harvest-area"),
                      requests: requests.slice() };
  reset();
  PREVIEW_FAILS = "task/task-9 is checked out outside Centrale at /repos/other -- git refuses" +
    " a second checkout of the same branch; finish or remove that worktree first";
  openOn("bad");
  clickNamed("drawer-harvest-area", "Discard attempt");
  return settle();
}).then(function () {
  rerender();
  out.externalRefusal = { lines: lines("drawer-harvest-area"),
                          requests: requests.slice() };
});

// -- task-121: the repository moved while the confirm sat armed --
var MOVED_TIP = "9c1f0aa2b3d4e5f60718293a4b5c6d7e8f901234";
chain = chain.then(function () {
  reset();
  PREVIEW = JSON.parse(JSON.stringify(BASE_PREVIEW));
  openOn("bad");
  clickNamed("drawer-harvest-area", "Discard attempt");
  return settle();
}).then(function () {
  rerender();
  out.stale = { armed: texts("drawer-harvest-area") };
  // A commit lands and a file is written: the server refuses the
  // already-armed click rather than destroying what it never named.
  POST_FAILS = "the repository changed after that preview -- task/task-9 has moved: it is at" +
    " 9c1f0aa2b3 now, not 1fb0261627. Nothing was discarded; check again what would go.";
  clickNamed("drawer-harvest-area", out.stale.armed[out.stale.armed.length - 1]);
  return settle();
}).then(function () {
  rerender();
  out.stale.posted = postBodies.slice();
  out.stale.afterLines = lines("drawer-harvest-area");
  out.stale.afterButtons = texts("drawer-harvest-area");
  // The way on is another first click: a fresh measurement, which now
  // reports the commit and the file that landed.
  POST_FAILS = null;
  PREVIEW = JSON.parse(JSON.stringify(BASE_PREVIEW));
  PREVIEW.branchTip = MOVED_TIP;
  PREVIEW.commitCount = 4;
  PREVIEW.dirtyPaths = ["a.txt", "b.txt", "c.txt", "d.txt", "e.txt"];
  PREVIEW.dirtyFileCount = 5;
  clickNamed("drawer-harvest-area", "Discard attempt");
  return settle();
}).then(function () {
  rerender();
  out.stale.rearmed = texts("drawer-harvest-area");
  out.stale.requests = requests.slice();
  clickNamed("drawer-harvest-area", out.stale.rearmed[out.stale.rearmed.length - 1]);
  return settle();
}).then(function () {
  rerender();
  out.stale.retryPosted = postBodies.slice();
  PREVIEW = JSON.parse(JSON.stringify(BASE_PREVIEW));
});

// -- a count that could not be measured never becomes a zero --
chain = chain.then(function () {
  reset();
  PREVIEW = JSON.parse(JSON.stringify(PREVIEW));
  PREVIEW.commitCount = null;
  openOn("bad");
  clickNamed("drawer-harvest-area", "Discard attempt");
  return settle();
}).then(function () {
  rerender();
  out.blindRefusal = { lines: lines("drawer-harvest-area"),
                       buttons: texts("drawer-harvest-area"),
                       requests: requests.slice() };
});

chain.then(function () {
  require("fs").writeSync(1, JSON.stringify(out));
  process.exit(0);
}).catch(function (e) {
  require("fs").writeSync(2, String(e && e.stack || e));
  process.exit(1);
});
"""


@js_harness.requires_node
class ThrowawayAttemptBehaviourTests(unittest.TestCase):
    """task-119's two ways out of a bad attempt, driven through the real
    sources: which branch states offer them, what the confirming click
    actually says, and what the page looks like once the branch is gone.

    The claim that matters most here is AC #4 -- "the confirming click
    names exactly what is being destroyed" -- and it is only meaningful
    if the numbers come from a measurement rather than from the button's
    own imagination. So the driver's `fetch` answers
    GET /api/discard-preview with a specific 3-commits/4-files
    measurement, and these tests read the sentence the second click is
    labelled with.
    """

    @property
    def out(self):
        return js_harness.cached_driver(self, THROWAWAY_DRIVER_JS)

    ABANDON = "Abandon worktree, keep branch"
    # task-125: short at rest; the sentence naming what would go belongs
    # to the armed state, which the confirm tests below read.
    DISCARD = "Discard attempt"
    # ...and the two spawn actions share the one secondary row with
    # them, ahead of them, since they are wanted far more often.
    RESUME = "Resume agent"
    RESPAWN = "Re-spawn agent"

    def test_only_a_live_unmerged_attempt_of_centrales_own_is_offered_both(self):
        offered = self.out["offered"]
        # A branch with a Centrale worktree: Merge, then the secondary
        # row -- what to do with the attempt, then both ways out of it.
        self.assertEqual(offered["bad"],
                         ["Merge", self.RESUME, self.RESPAWN, self.ABANDON, self.DISCARD])
        # Parked: there is no worktree to abandon (nor to resume into),
        # but the branch still needs deleting for the next spawn to
        # start from the base.
        self.assertEqual(offered["parked"], ["Merge", self.RESPAWN, self.DISCARD])

    def test_neither_action_is_offered_where_it_could_only_do_harm_or_nothing(self):
        offered = self.out["offered"]
        # Already merged: the work is on the base and "Merged -- clean
        # up" is the correct action; a forced delete would be a noisier
        # way to do the same thing. Re-spawn trails it in the secondary
        # row as it trails every primary (task-125).
        self.assertEqual(offered["merged"], ["Merged — clean up", self.RESPAWN])
        # Checked out elsewhere: the disabled Merge above already carries
        # that reason, and the server would 409 either way.
        self.assertEqual(offered["external"], ["Merge"])
        # A task with no branch has nothing to throw away -- the one
        # case in this area where an absent control is honest. (A live
        # session used to be a second one; see the task-120 test below.)
        self.assertEqual(offered["nobranch"], [])

    def test_a_live_session_disables_the_actions_rather_than_emptying_the_area(self):
        # task-120: this asserted an EMPTY area, which combined with the
        # session area's own silence on an "unknown" state to leave a
        # branch-bearing task with nothing at all to click. The actions
        # are still unreachable while an agent runs in the worktree --
        # they now say so.
        # No Resume or Re-spawn among them: the session IS the agent
        # this branch has, and End session above is the way out.
        self.assertEqual(self.out["offered"]["live"],
                         ["Merge", self.ABANDON, self.DISCARD])
        for btn in self.out["offeredButtons"]["live"]:
            with self.subTest(btn["text"]):
                self.assertIs(btn["disabled"], True)
                self.assertIn("centrale-my-tool-task-5", btn["title"])
                self.assertIn("End the session first", btn["title"])
        # ...and the reason is on screen, not only in a tooltip.
        self.assertEqual(len(self.out["offeredLines"]["live"]), 1)
        self.assertIn("still live: centrale-my-tool-task-5",
                      self.out["offeredLines"]["live"][0])
        # task-151: it names the refusal, not a status code. The three
        # actions do not share one: Abandon and Discard are refused
        # outright, Merge comes back 200 with harvest's first gate
        # failed, so any single number here is wrong for one of them.
        self.assertNotIn("409", self.out["offeredLines"]["live"][0])

    def test_the_first_click_measures_and_the_second_click_names_what_it_destroys(self):
        d = self.out["discard"]
        self.assertEqual(d["before"],
                         ["Merge", self.RESUME, self.RESPAWN, self.ABANDON, self.DISCARD])
        # First click: it says it is checking, and the only thing it
        # sends is the read-only preview.
        self.assertIn("Checking what would go…", d["whileMeasuring"])
        self.assertEqual(d["requestsAfterFirstClick"], ["GET /api/discard-preview"])
        # AC #4: the confirming label is the sentence the task asked for.
        self.assertEqual(d["armed"][-1]["text"],
                         "Discard 3 commits and 4 uncommitted files?")
        self.assertIn("confirming", d["armed"][-1]["className"])
        # The detail line spells out the halves that differ: the commits
        # survive through a tag, the uncommitted files do not.
        detail = [line for line in d["armedLines"] if "stay recoverable" in line]
        self.assertEqual(len(detail), 1, d["armedLines"])
        self.assertIn("Deletes task/task-9", detail[0])
        self.assertIn("3 commits over main", detail[0])
        self.assertIn("4 uncommitted files do not", detail[0])

    def test_the_second_click_posts_once_and_surfaces_the_recovery_command(self):
        d = self.out["discard"]
        self.assertEqual(d["requests"],
                         ["GET /api/discard-preview", "POST /api/discard-attempt"])
        # AC #5: the SHA-bearing command is on screen and copyable, not
        # just in a toast that expires in six seconds.
        self.assertEqual(
            d["afterCodes"],
            ["git branch task/task-9 1fb0261627d96454a5d478560631e4bbdc9bd015"])
        self.assertEqual(len(d["afterLines"]), 1)
        self.assertIn("Discarded task/task-9", d["afterLines"][0])
        self.assertIn("3 commits and 4 uncommitted files gone", d["afterLines"][0])
        self.assertIn("abandoned/task-9-20260904-163012", d["afterLines"][0])
        # The buttons go the instant it lands -- only the Copy button of
        # the recovery row is left.
        self.assertEqual(d["afterButtons"], ["Copy"])

    def test_a_discarded_task_gets_the_ordinary_spawn_button_back(self):
        # AC #3, the whole point of deleting the branch rather than
        # parking it: before, the drawer offers only Resume/Re-spawn into
        # the existing worktree; after, it offers a fresh Spawn.
        d = self.out["discard"]
        # task-125: before, Resume/Re-spawn are members of the action
        # area's secondary row (see d["before"] above) and this area is
        # empty; after, the branch is gone and the fresh Spawn button is
        # what this area is for.
        self.assertEqual(d["spawnBefore"], [])
        self.assertEqual(d["spawnAfter"], ["Spawn agent"])
        # And the board refetch that follows agrees rather than undoing
        # it -- while the recovery command stays on screen, which is the
        # reason the outcome line outlives the branch it describes.
        self.assertEqual(d["spawnAfterRefetch"], ["Spawn agent"])
        self.assertEqual(
            d["codesAfterRefetch"],
            ["git branch task/task-9 1fb0261627d96454a5d478560631e4bbdc9bd015"])

    def test_abandon_names_only_the_uncommitted_files_and_keeps_the_branch(self):
        a = self.out["abandon"]
        self.assertEqual(a["armed"][-2], "Remove the worktree and discard 4 uncommitted files?")
        detail = [line for line in a["armedLines"] if "still be merged later" in line]
        self.assertEqual(len(detail), 1, a["armedLines"])
        self.assertIn("The 3 commits on task/task-9 stay", detail[0])
        self.assertEqual(a["requests"],
                         ["GET /api/discard-preview", "POST /api/abandon-worktree"])
        self.assertIn("Removed the worktree for task/task-9", a["afterLines"][0])
        self.assertIn("Its 3 commits stay on the parked branch", a["afterLines"][0])
        # The branch survived, so the branch-bearing spawn actions did
        # too -- in the secondary row, which is where they live since
        # task-125; this area stays empty for a branch.
        self.assertEqual(a["spawnAfter"], [])
        self.assertEqual(a["afterButtons"][1:3], [self.RESUME, self.RESPAWN])

    def test_an_abandoned_branch_keeps_its_merge_and_loses_only_the_abandon(self):
        # Parked and still mergeable is the whole point of the milder
        # action, so unlike a discard it must not take Merge away with
        # it. The abandon button itself goes: there is no worktree left
        # to abandon, and its click could only 404.
        a = self.out["abandon"]
        self.assertEqual(a["afterButtons"],
                         ["Merge", self.RESUME, self.RESPAWN, self.DISCARD])
        # The refetch reports the branch as parked: no worktree to
        # resume into either, so Re-spawn stands alone beside Discard.
        self.assertEqual(a["buttonsAfterRefetch"], ["Merge", self.RESPAWN, self.DISCARD])

    def test_a_refused_preview_never_arms_and_never_posts(self):
        # AC #1/#8 from the page's side: the server's refusal reaches the
        # user as the reason, and the click that would destroy something
        # is never armed.
        live = self.out["liveRefusal"]
        self.assertEqual(live["requests"], ["GET /api/discard-preview"])
        self.assertEqual(
            live["lines"],
            ["Discard refused: a live tmux session is still running: centrale-my-tool-task-1"])
        self.assertIn(self.DISCARD, live["buttons"])  # unarmed, ready to try again

        ext = self.out["externalRefusal"]
        self.assertEqual(ext["requests"], ["GET /api/discard-preview"])
        self.assertIn("checked out outside Centrale at /repos/other", ext["lines"][0])

    def test_the_destructive_click_sends_the_state_the_confirm_was_built_from(self):
        # task-121: the second click is bound to the measurement the
        # first one made. Without these two fields the POST states no
        # expectation, and the server has nothing to compare a moved
        # repository against.
        tip = "1fb0261627d96454a5d478560631e4bbdc9bd015"
        for case, url in (("discard", "/api/discard-attempt"),
                          ("abandon", "/api/abandon-worktree")):
            with self.subTest(case):
                posted = self.out[case]["posted"]
                self.assertEqual(len(posted), 1, posted)
                self.assertEqual(posted[0]["url"], url)
                self.assertEqual(posted[0]["body"], {
                    "project": "my-tool", "taskId": "TASK-1",
                    "expectedBranchTip": tip,
                    "expectedDirtyPaths": ["a.txt", "b.txt", "c.txt", "d.txt"],
                })

    def test_a_repository_that_moved_costs_a_fresh_preview_with_the_new_numbers(self):
        # task-121, the whole loop: the armed confirm named 3 commits and
        # 4 files, a commit and a file landed, the server refused the
        # click, and the only way on is another measurement -- whose
        # confirm names 4 and 5 rather than repeating the stale sentence.
        stale = self.out["stale"]
        self.assertEqual(stale["armed"][-1], "Discard 3 commits and 4 uncommitted files?")
        self.assertEqual(stale["posted"][0]["body"]["expectedBranchTip"],
                         "1fb0261627d96454a5d478560631e4bbdc9bd015")

        # The refusal is shown as the reason, and nothing stays armed.
        self.assertEqual(len(stale["afterLines"]), 1, stale["afterLines"])
        self.assertIn("task/task-9 has moved", stale["afterLines"][0])
        self.assertIn("Nothing was discarded", stale["afterLines"][0])
        self.assertEqual(stale["afterButtons"][-1], self.DISCARD)

        # A second first-click re-measures...
        self.assertEqual(stale["requests"], [
            "GET /api/discard-preview", "POST /api/discard-attempt",
            "GET /api/discard-preview",
        ])
        self.assertEqual(stale["rearmed"][-1], "Discard 4 commits and 5 uncommitted files?")
        # ...and the click that follows it is bound to THAT measurement.
        self.assertEqual(len(stale["retryPosted"]), 2, stale["retryPosted"])
        self.assertEqual(stale["retryPosted"][-1]["body"]["expectedBranchTip"],
                         "9c1f0aa2b3d4e5f60718293a4b5c6d7e8f901234")
        self.assertEqual(stale["retryPosted"][-1]["body"]["expectedDirtyPaths"],
                         ["a.txt", "b.txt", "c.txt", "d.txt", "e.txt"])

    def test_a_count_it_could_not_measure_stops_the_confirm_rather_than_reading_zero(self):
        blind = self.out["blindRefusal"]
        self.assertEqual(blind["requests"], ["GET /api/discard-preview"])
        self.assertEqual(
            blind["lines"],
            ["Discard refused: Centrale could not measure what this would destroy"
             " -- refusing to confirm blind."])
        self.assertIn(self.DISCARD, blind["buttons"])


# ---------------------------------------------------------------------
# task-97: the drawer's self-announcing sections
# ---------------------------------------------------------------------

# task-97: the driver for the drawer's self-announcing sections. Runs the
# REAL static/ sources (state.js -> drawer.js) over the shared DOM shim in
# tests/js_harness.py, opens a drawer on a big ticket, folds a
# section by clicking its real header button, and re-opens it -- so what
# the assertions below read is what a browser would have rendered.
DRAWER_SECTIONS_DRIVER_JS = r"""
var fs = require("fs");
var path = require("path");
var vm = require("vm");

var STATIC = process.argv[1];
["state.js", "dom.js", "tasks.js", "board.js", "spawn.js", "harvest.js", "drawer.js"].forEach(function (name) {
  vm.runInThisContext(fs.readFileSync(path.join(STATIC, name), "utf8"), { filename: name });
});
var C = window.Centrale;

// The files this harness does not load, as inert sinks -- the drawer's
// pane polling in particular, which task-97 must not touch.
var paneSyncCalls = 0;
C.syncDrawerPanePolling = function () { paneSyncCalls++; };
C.renderBoard = function () {};
C.renderSessionsPanel = function () {};
C.showToast = function () {};

var VIEW = JSON.parse(process.argv[2]);
global.fetch = function () {
  return Promise.resolve({ ok: true, status: 200, json: function () { return Promise.resolve(VIEW); } });
};

var SUMMARY = {
  id: "TASK-2", title: "A big ticket", status: "In Progress",
  ready: false, hasSpawnBranch: false, alreadyMerged: false
};
var PROJECT = { name: "my-app", tasks: [SUMMARY], statuses: ["To Do", "In Progress", "Done"] };
C.knownProjects.length = 0;
C.knownProjects.push("my-app");
C.boardData = { projects: [PROJECT], capabilities: { tmux: true } };
C.sessionsData = [];

function sections() {
  return document.getElementById("drawer-body").childNodes
    .filter(function (n) { return n.attrs && n.attrs["data-section"]; })
    .map(function (s) {
      var head = s.childNodes[0];
      var toggle = head.childNodes[0];
      var body = s.childNodes[1];
      return {
        key: s.attrs["data-section"],
        header: head.textContent,
        collapsed: s.className.indexOf("collapsed") !== -1,
        ariaExpanded: toggle.attrs["aria-expanded"],
        controls: toggle.attrs["aria-controls"],
        bodyId: body.attrs.id,
        bodyText: body.textContent
      };
    });
}

function toggleOf(key) {
  var hit = document.getElementById("drawer-body").childNodes.filter(function (n) {
    return n.attrs && n.attrs["data-section"] === key;
  });
  if (hit.length !== 1) throw new Error("expected 1 " + key + " section, got " + hit.length);
  return hit[0].childNodes[0].childNodes[0];
}

function tick() { return new Promise(function (r) { setTimeout(r, 0); }); }

var out = {};
C.openDrawer(PROJECT, SUMMARY);
tick().then(function () {
  out.initial = sections();
  out.fadeIsLastChild = document.getElementById("drawer-body")
    .childNodes.slice(-1)[0].className;
  // Unfold the long description and fold the dependencies: both through
  // the real header button a viewer clicks, in both directions.
  toggleOf("description").click();
  toggleOf("dependencies").click();
  out.afterClicks = sections();
  out.stored = window.localStorage.getItem("centrale-drawer-sections");
  // Re-opening the drawer must honour those choices over the defaults.
  C.openDrawer(PROJECT, SUMMARY);
  return tick();
}).then(function () {
  out.reopened = sections().map(function (s) {
    return { key: s.key, collapsed: s.collapsed };
  });
  out.paneSyncCalls = paneSyncCalls;
  process.stdout.write(JSON.stringify(out));
});
"""


class DrawerSectionRenderTests(unittest.TestCase):
    """What a big ticket and a small one actually look like when opened.

    Task-97's counts, folds and aria wiring, read off the DOM the real
    render path built, plus the fold surviving a re-open. The CSS rules
    the fold rides on -- the flex model, the pane ceiling, the fade --
    stay source-text in test_server.py's DrawerSectionDiscoverabilityTests:
    this shim has no layout engine, so nothing here can observe them.
    """

    LONG_DESCRIPTION = " ".join(["word"] * 175)
    SHORT_TEXT = "Two short lines.\nNothing that needs folding."

    def _run_driver(self, task):
        # The shared shim in tests/js_harness.py, plus this task's own
        # driver -- see DRAWER_SECTIONS_DRIVER_JS above.
        payload = json.dumps({"task": task, "branchTask": None})
        return js_harness.run_driver(self, DRAWER_SECTIONS_DRIVER_JS, payload, timeout=30)

    def _big_task(self):
        return {
            "id": "TASK-2",
            "title": "A big ticket",
            "status": "In Progress",
            "description": self.LONG_DESCRIPTION,
            "acceptanceCriteria": [
                {"index": i, "text": "criterion %d" % i, "checked": i <= 2}
                for i in range(1, 7)
            ],
            "dependencies": ["TASK-1"],
            "implementationPlan": self.SHORT_TEXT,
            "implementationNotes": " ".join(["note"] * 130),
        }

    @js_harness.requires_node
    def test_a_big_ticket_opens_with_every_header_and_its_count_showing(self):
        out = self._run_driver(self._big_task())
        self.assertEqual(
            [(s["key"], s["header"]) for s in out["initial"]],
            [
                ("description", "Description175 words"),
                ("acceptanceCriteria", "Acceptance Criteria2/6"),
                ("dependencies", "Dependencies1"),
                ("implementationPlan", "Implementation Plan7 words"),
                ("implementationNotes", "Implementation Notes130 words"),
            ])
        # The long ones start folded so they cannot bury the headers
        # under them; the short ones are left open.
        self.assertEqual(
            {s["key"]: s["collapsed"] for s in out["initial"]},
            {
                "description": True,
                "acceptanceCriteria": True,
                "dependencies": False,
                "implementationPlan": False,
                "implementationNotes": True,
            })
        self.assertEqual(
            [s["ariaExpanded"] for s in out["initial"]],
            ["false", "false", "true", "true", "false"])
        self.assertEqual([s["controls"] for s in out["initial"]],
                         [s["bodyId"] for s in out["initial"]])
        # AC #4: folding hides, it does not drop -- every criterion, the
        # dependency and both long texts are still rendered in full.
        by_key = {s["key"]: s["bodyText"] for s in out["initial"]}
        self.assertEqual(by_key["description"], self.LONG_DESCRIPTION)
        for i in range(1, 7):
            self.assertIn("criterion %d" % i, by_key["acceptanceCriteria"])
        self.assertIn("TASK-1", by_key["dependencies"])
        self.assertIn("note note", by_key["implementationNotes"])
        # The scroll cue is the last thing in the body, after the ticket.
        self.assertEqual(out["fadeIsLastChild"], "drawer-body-fade")
        # And opening a drawer still drives the pane poller exactly once.
        self.assertEqual(out["paneSyncCalls"], 2)  # two openDrawer calls

    @js_harness.requires_node
    def test_a_clicked_header_folds_that_section_and_is_remembered(self):
        out = self._run_driver(self._big_task())
        after = {s["key"]: s["collapsed"] for s in out["afterClicks"]}
        self.assertIs(after["description"], False)    # unfolded by the click
        self.assertIs(after["dependencies"], True)    # folded by the click
        aria = {s["key"]: s["ariaExpanded"] for s in out["afterClicks"]}
        self.assertEqual(aria["description"], "true")
        self.assertEqual(aria["dependencies"], "false")
        # Only the two sections actually toggled are remembered: every
        # other section keeps following the size-derived default.
        self.assertEqual(
            json.loads(out["stored"]),
            {"description": False, "dependencies": True})
        # The next drawer open honours the viewer over the heuristic --
        # a 175-word description stays open because they opened it.
        self.assertEqual(
            {s["key"]: s["collapsed"] for s in out["reopened"]},
            {
                "description": False,
                "acceptanceCriteria": True,
                "dependencies": True,
                "implementationPlan": False,
                "implementationNotes": True,
            })

    @js_harness.requires_node
    def test_a_small_ticket_still_opens_with_everything_showing(self):
        out = self._run_driver({
            "id": "TASK-2",
            "title": "A small ticket",
            "status": "To Do",
            "description": self.SHORT_TEXT,
            "acceptanceCriteria": [{"index": 1, "text": "one", "checked": False}],
            "dependencies": [],
            "implementationPlan": None,
            "implementationNotes": None,
        })
        self.assertEqual(
            [(s["key"], s["header"], s["collapsed"]) for s in out["initial"]],
            [
                ("description", "Description7 words", False),
                ("acceptanceCriteria", "Acceptance Criteria0/1", False),
                ("dependencies", "Dependencies", False),
            ])
        # An empty section says so in its body rather than in a count.
        self.assertEqual(
            {s["key"]: s["bodyText"] for s in out["initial"]}["dependencies"], "None.")


# ---------------------------------------------------------------------
# Task-96: the merge button says which gate it is running
# ---------------------------------------------------------------------

# Drives the real static/ sources through a merge whose POST is held
# open, firing the poll timer by hand so a "second" costs nothing, over
# the shared DOM/window shim in tests/js_harness.py.
MERGE_PROGRESS_DRIVER_JS = r"""
var fs = require("fs");
var path = require("path");
var vm = require("vm");

var STATIC = process.argv[1];
["state.js", "dom.js", "tasks.js", "board.js", "spawn.js", "harvest.js", "drawer.js"].forEach(function (name) {
  vm.runInThisContext(fs.readFileSync(path.join(STATIC, name), "utf8"), { filename: name });
});
var C = window.Centrale;

C.renderBoard = function () {};
C.renderSessionsPanel = function () {};
C.doRefresh = function () {};
C.fetchSessions = function () {};
C.showToast = function () {};
C.openDrawer = function () {};

// The sources' own setTimeout is replaced so the ~1s poll can be fired
// on demand; the real one is kept for the driver's own microtask ticks.
var realSetTimeout = setTimeout;
function tick() { return new Promise(function (r) { realSetTimeout(r, 0); }); }
function settle() { return tick().then(tick).then(tick); }

var timers = [];
global.setTimeout = function (fn, ms) {
  var t = { fn: fn, ms: ms, dead: false };
  timers.push(t);
  return t;
};
global.clearTimeout = function (t) { if (t) t.dead = true; };
function firePolls() {
  var due = timers.filter(function (t) { return !t.dead && t.ms === 1000; });
  timers = timers.filter(function (t) { return due.indexOf(t) === -1; });
  due.forEach(function (t) { t.fn(); });
  return settle();
}

var requests = [];
var PROGRESS = null;
var resolveHarvest = null;
global.fetch = function (url, opts) {
  requests.push(url + " " + ((opts && opts.method) || "GET"));
  if (url === "/api/harvest") {
    // Held open: this is exactly the multi-second window the feature
    // exists for.
    return new Promise(function (resolve) { resolveHarvest = resolve; });
  }
  if (url === "/api/harvest-progress") {
    return Promise.resolve({
      ok: true, status: 200,
      json: function () { return Promise.resolve({ progress: PROGRESS }); }
    });
  }
  return Promise.resolve({ ok: true, status: 200, json: function () { return Promise.resolve({}); } });
};
function landMerge(data) {
  var resolve = resolveHarvest;
  resolveHarvest = null;
  resolve({ ok: true, status: 200, json: function () { return Promise.resolve(data); } });
  return settle();
}

var MERGED = { merged: true, taskId: "TASK-77", branch: "task/TASK-77", baseBranch: "main", gates: [] };

function board() {
  var task = {
    id: "TASK-77", title: "A task", status: "In Progress", ready: true,
    hasSpawnBranch: true, alreadyMerged: false, worktreeDirty: false,
    branchCheckout: { kind: "centrale" }
  };
  C.knownProjects.length = 0;
  C.knownProjects.push("my-tool");
  C.boardData = { projects: [{ name: "my-tool", tasks: [task] }], capabilities: { tmux: true } };
  C.sessionsData = [];
  C.currentDrawer = { project: "my-tool", id: "TASK-77", summary: task, branchTask: null };
  [C.harvestStates, C.spawnStates, C.endSessionStates].forEach(function (map) {
    Object.keys(map).forEach(function (k) { delete map[k]; });
  });
  C.harvestAllInFlight = false;
  requests.length = 0;
  PROGRESS = null;
}

function lines(id) {
  return document.getElementById(id).childNodes.map(function (n) {
    return n.tagName + "|" + n.className + "|" + n.textContent;
  });
}
function mergeButton() {
  // The primary one specifically: task-119 put two secondary throwaway
  // actions in this area, and they are not what this driver is about.
  var hit = document.getElementById("drawer-harvest-area").childNodes.filter(function (n) {
    return n.tagName === "button" && String(n.className).indexOf("btn-primary") !== -1;
  });
  if (hit.length !== 1) throw new Error("expected 1 primary button, got " + hit.length);
  return hit[0];
}
function progressFor(gate, taskId, project) {
  return {
    project: project || "my-tool", taskId: taskId || "TASK-77",
    branch: "task/task-77", trigger: "click", gate: gate
  };
}

var out = {};

// 1. A slow merge: the button names each gate the server reports, most
//    importantly the checkCommand one.
function namesTheLiveGate() {
  board();
  C.renderDrawerHarvestArea();
  mergeButton().click();
  return settle().then(function () {
    out.beforeAnyPoll = mergeButton().textContent;
    PROGRESS = progressFor("noLiveSession");
    return firePolls();
  }).then(function () {
    out.onFirstGate = mergeButton().textContent;
    PROGRESS = progressFor("checkCommand");
    return firePolls();
  }).then(function () {
    out.onCheckCommand = mergeButton().textContent;
    PROGRESS = progressFor("merge");
    return firePolls();
  }).then(function () {
    out.onMerge = mergeButton().textContent;
    out.requestsDuringMerge = requests.slice();
    return landMerge(MERGED);
  }).then(function () {
    C.renderDrawerHarvestArea();
    out.afterMerge = lines("drawer-harvest-area");
    var before = requests.length;
    return firePolls().then(function () {
      out.pollsAfterLanding = requests.length - before;
    });
  });
}

// 2. Every "we cannot say" case falls back to the plain label.
function fallsBackWhenItCannotSay() {
  board();
  C.renderDrawerHarvestArea();
  mergeButton().click();
  return settle().then(function () {
    PROGRESS = null;                               // nothing published yet
    return firePolls();
  }).then(function () {
    out.noProgress = mergeButton().textContent;
    PROGRESS = progressFor("checkCommand", "TASK-78");   // someone else's merge
    return firePolls();
  }).then(function () {
    out.otherTask = mergeButton().textContent;
    PROGRESS = progressFor("checkCommand", "TASK-77", "my-lib");  // other project
    return firePolls();
  }).then(function () {
    out.otherProject = mergeButton().textContent;
    PROGRESS = progressFor("somethingNewer");            // unknown stage name
    return firePolls();
  }).then(function () {
    out.unknownStage = mergeButton().textContent;
    return landMerge(MERGED);
  });
}

// 3. A merge with no checkCommand lands well inside the first poll
//    interval -- no poll is ever made, so no label can flash.
function fastMergeNeverPolls() {
  board();
  PROGRESS = progressFor("checkCommand");
  C.renderDrawerHarvestArea();
  mergeButton().click();
  return settle().then(function () {
    return landMerge(MERGED);
  }).then(function () {
    return firePolls();
  }).then(function () {
    out.fastMergeRequests = requests.slice();
  });
}

// 4. "Merge all ready" gets the same treatment, matched on the project
//    its own request is currently walking.
function mergeAllReady() {
  board();
  var btn = document.getElementById("harvest-all-btn");
  C.harvestAllReady();
  return settle().then(function () {
    out.allBefore = btn.textContent;
    PROGRESS = progressFor("checkCommand");
    return firePolls();
  }).then(function () {
    out.allDuring = btn.textContent;
    PROGRESS = progressFor("checkCommand", "TASK-1", "my-lib");
    return firePolls();
  }).then(function () {
    out.allOtherProject = btn.textContent;
    return landMerge({ merged: [], notReady: [] });
  }).then(function () {
    out.allAfter = btn.textContent;
    var before = requests.length;
    return firePolls().then(function () {
      out.allPollsAfterLanding = requests.length - before;
    });
  });
}

namesTheLiveGate()
  .then(fallsBackWhenItCannotSay)
  .then(fastMergeNeverPolls)
  .then(mergeAllReady)
  .then(function () { process.stdout.write(JSON.stringify(out)); });
"""


class MergeGateProgressBehaviourTests(unittest.TestCase):
    """The Merge button tracked through a POST held open by hand.

    Which label the button carries at each moment is behaviour: the
    driver holds /api/harvest open, publishes one stage at a time and
    fires the progress timer, and the assertions read the button. The
    label TABLE, and the rule that none of its entries can contain a
    digit or a clock, stay source-text in test_server.py's
    MergeGateProgressTests -- a run only visits the stages it is fed,
    where that rule is about every entry there will ever be.
    """

    @js_harness.requires_node
    def test_the_button_tracks_the_server_through_a_held_open_merge(self):
        out = js_harness.run_driver(self, MERGE_PROGRESS_DRIVER_JS)

        # AC #1: the gate currently being evaluated, named on the button.
        self.assertEqual(out["beforeAnyPoll"], "Merging…")
        self.assertEqual(out["onFirstGate"], "Checking session…")
        self.assertEqual(out["onCheckCommand"], "Running tests…")
        self.assertEqual(out["onMerge"], "Merging…")
        # AC #5: one POST, then only progress reads while it is open.
        self.assertEqual(out["requestsDuringMerge"], [
            "/api/harvest POST",
            "/api/harvest-progress GET",
            "/api/harvest-progress GET",
            "/api/harvest-progress GET",
        ])
        # ...and not one more read after the response lands.
        self.assertEqual(out["pollsAfterLanding"], 0)
        self.assertEqual(out["afterMerge"],
                         ["div|spawn-status-line success|Merged task/TASK-77 into main"])

        # AC #3: every "cannot say" case is today's plain label, never a
        # guess -- nothing published, another task's merge, another
        # project's merge, and a stage name this frontend doesn't know.
        self.assertEqual(out["noProgress"], "Merging…")
        self.assertEqual(out["otherTask"], "Merging…")
        self.assertEqual(out["otherProject"], "Merging…")
        self.assertEqual(out["unknownStage"], "Merging…")

        # AC #6: a merge that lands inside the first interval (every
        # project without a checkCommand) polls exactly zero times, so
        # there is nothing that could flash a label.
        self.assertEqual(out["fastMergeRequests"], ["/api/harvest POST"])

        # "Merge all ready" reports the project it is currently walking,
        # and stops with its own request the same way.
        self.assertEqual(out["allBefore"], "Merging…")
        self.assertEqual(out["allDuring"], "Running tests…")
        self.assertEqual(out["allOtherProject"], "Merging…")
        self.assertEqual(out["allAfter"], "Merge all ready")
        self.assertEqual(out["allPollsAfterLanding"], 0)


# ---------------------------------------------------------------------
# task-116: Resume, for every shape an interrupted task comes in
# ---------------------------------------------------------------------

# task-116: the driver for the drawer's Resume-vs-Re-spawn decision.
# Runs the REAL static/ sources over the shared DOM shim in
# tests/js_harness.py, and reads the buttons renderDrawerSpawnArea
# actually rendered for each branch state.
RESUME_WHEN_ACTIVE_DRIVER_JS = r"""
var fs = require("fs");
var path = require("path");
var vm = require("vm");

var STATIC = process.argv[1];
["state.js", "dom.js", "tasks.js", "board.js", "spawn.js", "harvest.js", "drawer.js"].forEach(function (name) {
  vm.runInThisContext(fs.readFileSync(path.join(STATIC, name), "utf8"), { filename: name });
});
var C = window.Centrale;

// The files this harness does not load, as inert sinks.
C.renderBoard = function () {};
C.renderSessionsPanel = function () {};
C.doRefresh = function () {};
C.fetchSessions = function () {};
C.showToast = function () {};
C.syncDrawerPanePolling = function () {};

var requests = [];
var VIEW = null;
global.fetch = function (url, opts) {
  requests.push(url + " " + ((opts && opts.method) || "GET"));
  var data = url === "/api/task" || url.indexOf("/api/task?") === 0
    ? VIEW
    : { session: "centrale-my-tool-TASK-46", attach: "tmux attach", agent: "claude" };
  return Promise.resolve({ ok: true, status: 200, json: function () { return Promise.resolve(data); } });
};

var STATUSES = ["To Do", "In Progress", "Done"];

// A task/<id> branch, no live session, and whatever worktree/branch-side
// status the scenario is about. `dirty` is the ONLY thing task-116
// widened away from, so every scenario states it explicitly.
function setup(opts) {
  var task = {
    id: "TASK-46", title: "A task", status: "In Progress", ready: true,
    hasSpawnBranch: true, alreadyMerged: false,
    worktreeDirty: !!opts.dirty,
    branchCheckout: { kind: "centrale" }
  };
  var project = { name: "my-tool", tasks: [task], statuses: opts.statuses || STATUSES };
  C.knownProjects.length = 0;
  C.knownProjects.push("my-tool");
  C.boardData = { projects: [project], capabilities: { tmux: true } };
  C.sessionsData = [];
  C.currentDrawer = { project: "my-tool", id: "TASK-46", summary: task };
  if ("branchStatus" in opts) {
    C.currentDrawer.branchTask = opts.branchStatus === null
      ? null
      : { id: "TASK-46", status: opts.branchStatus };
  }
  [C.harvestStates, C.spawnStates, C.cleanupStates, C.endSessionStates, C.spawnConfirmPending]
    .forEach(function (map) { Object.keys(map).forEach(function (k) { delete map[k]; }); });
  requests.length = 0;
  return { task: task, project: project };
}

// task-125: Resume and Re-spawn are members of the drawer action
// area's ONE secondary row now, so that row is what these scenarios
// read -- the same two buttons, in the container they actually render
// in, alongside the two throwaway actions they share it with.
function row() {
  var found = document.getElementById("drawer-harvest-area").childNodes
    .filter(function (n) { return n.className === "drawer-action-row"; });
  return found.length ? found[0].childNodes : [];
}
function area() {
  return row().map(function (n) {
    return n.tagName + "|" + n.className + "|" + n.textContent;
  });
}
function buttons() {
  return row().filter(function (n) { return n.tagName === "button"; });
}
function button(text) {
  var hit = buttons().filter(function (n) { return n.textContent === text; });
  if (hit.length !== 1) throw new Error("expected 1 " + JSON.stringify(text) + " button, got " + hit.length);
  return hit[0];
}
function tick() { return new Promise(function (r) { setTimeout(r, 0); }); }

var out = {};

// 1. The reported case: the agent committed, THEN was killed. Clean
//    worktree, branch-side task still In Progress, no session.
setup({ dirty: false, branchStatus: "In Progress" });
C.renderDrawerHarvestArea();
out.committedThenKilled = area();
out.committedThenKilledTitles = buttons().map(function (b) { return b.title; });

// 2. A dirty worktree, unchanged: Resume, whatever the branch status is.
setup({ dirty: true, branchStatus: "Done" });
C.renderDrawerHarvestArea();
out.dirtyDoneBranch = area();
setup({ dirty: true, branchStatus: null });
C.renderDrawerHarvestArea();
out.dirtyNoBranchTask = area();

// 3. A finished branch is a merge candidate, not a resume candidate.
setup({ dirty: false, branchStatus: "Done" });
C.renderDrawerHarvestArea();
out.doneBranch = area();
setup({ dirty: false, branchStatus: "done" });   // statuses are compared case-insensitively
C.renderDrawerHarvestArea();
out.doneBranchLowercase = area();

// 4. Configurable statuses: the repo's own first column is "not started
//    yet", its own middle column is active. No status name is assumed.
var CUSTOM = ["Backlog", "Doing", "Shipped"];
setup({ dirty: false, branchStatus: "Backlog", statuses: CUSTOM });
C.renderDrawerHarvestArea();
out.customFirstColumn = area();
setup({ dirty: false, branchStatus: "Doing", statuses: CUSTOM });
C.renderDrawerHarvestArea();
out.customActive = area();

// 5. Nothing read yet (the render before /api/task lands) and nothing
//    to read (no branch-side copy at all): today's answer, not a guess.
setup({ dirty: false });
C.renderDrawerHarvestArea();
out.beforeFetch = area();
setup({ dirty: false, branchStatus: null });
C.renderDrawerHarvestArea();
out.noBranchTask = area();
setup({ dirty: false, branchStatus: "" });
C.renderDrawerHarvestArea();
out.emptyBranchStatus = area();

// 6. Arming Resume must not arm Re-spawn, and confirming Resume must hit
//    /api/resume -- the two share one pending entry per task.
setup({ dirty: false, branchStatus: "In Progress" });
C.renderDrawerHarvestArea();
button("Resume agent").click();
out.afterArmingResume = area();
button("Re-spawn agent").click();          // arms Re-spawn for itself instead
out.afterClickingRespawn = area();
button("Confirm re-spawn?").click();
out.requestsAfterConfirmedRespawn = requests.slice();

// 7. The whole drawer open, driven through the real fetch: the first
//    pass has no branch status, the second does. Runs after a tick, so
//    scenario 6's in-flight POST cannot land in this scenario's state.
tick().then(function () {
  setup({ dirty: false });
  VIEW = {
    task: { id: "TASK-46", title: "A task", status: "In Progress" },
    branchTask: { task: { id: "TASK-46", status: "In Progress" } }
  };
  var project = C.boardData.projects[0];
  C.openDrawer(project, project.tasks[0]);
  out.openFirstPass = area();
  return tick();
}).then(function () {
  out.openAfterFetch = area();
  out.openRequests = requests.filter(function (r) { return r.indexOf("/api/task") === 0; }).length;
  fs.writeSync(1, JSON.stringify(out));
  process.exit(0);
});
"""


class ResumeWhenActiveBehaviourTests(unittest.TestCase):
    """The buttons renderDrawerSpawnArea renders, per branch state.

    Nine branch shapes and two confirm arms, read as the rendered
    buttons rather than as the spelling of the condition that chose
    them. The condition itself -- that it names no status of its own,
    reads the branch's copy and not main's, and is re-run when
    /api/task lands -- stays source-text in test_server.py's
    ResumeAnInterruptedActiveTaskTests.
    """

    # -- AC #1/#2/#3/#4/#6: the rendered buttons --

    @js_harness.requires_node
    def test_the_drawer_offers_resume_for_every_interrupted_shape(self):
        out = js_harness.run_driver(self, RESUME_WHEN_ACTIVE_DRIVER_JS)

        RESUME = "button|btn btn-sm|Resume agent"
        SECONDARY_RESPAWN = "button|btn btn-sm btn-quiet|Re-spawn agent"
        # task-125: every scenario here is an unmerged branch with a
        # Centrale worktree, so the two ways out of a bad attempt share
        # the row with them, last and quiet.
        WAYS_OUT = ["button|btn btn-sm btn-quiet|Abandon worktree, keep branch",
                    "button|btn btn-sm btn-quiet|Discard attempt"]
        RESPAWN_ONLY = ["button|btn btn-sm|Re-spawn agent"] + WAYS_OUT

        # AC #6 / AC #1: committed, then killed -- clean worktree, branch
        # status In Progress, no session. Resume first, Re-spawn under it.
        self.assertEqual(out["committedThenKilled"], [RESUME, SECONDARY_RESPAWN] + WAYS_OUT)
        self.assertTrue(all(out["committedThenKilledTitles"]), "both buttons say what they do")

        # AC #2: a dirty worktree still reaches Resume by itself -- even
        # with a Done branch copy, and even before one has been read.
        self.assertEqual(out["dirtyDoneBranch"], [RESUME, SECONDARY_RESPAWN] + WAYS_OUT)
        self.assertEqual(out["dirtyNoBranchTask"], [RESUME, SECONDARY_RESPAWN] + WAYS_OUT)

        # AC #3: a Done branch is a merge candidate -- untouched.
        self.assertEqual(out["doneBranch"], RESPAWN_ONLY)
        self.assertEqual(out["doneBranchLowercase"], RESPAWN_ONLY)

        # AC #4: a repo's own columns decide, not the canonical names.
        self.assertEqual(out["customFirstColumn"], RESPAWN_ONLY)
        self.assertEqual(out["customActive"], [RESUME, SECONDARY_RESPAWN] + WAYS_OUT)

        # A status nobody has read is not an active status.
        self.assertEqual(out["beforeFetch"], RESPAWN_ONLY)
        self.assertEqual(out["noBranchTask"], RESPAWN_ONLY)
        self.assertEqual(out["emptyBranchStatus"], RESPAWN_ONLY)

        # The two confirms are independent, and confirming Re-spawn posts
        # to /api/spawn -- never to /api/resume, whose button was armed
        # first.
        self.assertEqual(out["afterArmingResume"],
                         ["button|btn btn-sm confirming|Confirm resume?",
                          SECONDARY_RESPAWN] + WAYS_OUT)
        self.assertEqual(out["afterClickingRespawn"],
                         [RESUME, "button|btn btn-sm btn-quiet confirming|Confirm re-spawn?"]
                         + WAYS_OUT)
        self.assertEqual(out["requestsAfterConfirmedRespawn"], ["/api/spawn POST"])

        # Opening the drawer: the pass before /api/task lands keeps
        # today's answer, and the pass that has the branch status offers
        # Resume -- one fetch, two renders.
        self.assertEqual(out["openFirstPass"], RESPAWN_ONLY)
        self.assertEqual(out["openAfterFetch"], [RESUME, SECONDARY_RESPAWN] + WAYS_OUT)
        self.assertEqual(out["openRequests"], 1)


# ---------------------------------------------------------------------
# The footer's version credit (task-107)
# ---------------------------------------------------------------------

# What the sidebar footer ends up showing, driven through the real
# fetch -> render path: the board response is the ONLY source of the
# value, so a client that derived, defaulted or cached a version of its
# own would show up here as a footer that says something the server
# never sent.
VERSION_DRIVER_JS = js_harness.LOAD_SOURCES_JS + r"""
// api.js arms the refresh countdown at load. The driver owns timers, so
// nothing real is scheduled and node's loop is never held open.
global.setInterval = function (fn, ms) { return { fn: fn, ms: ms }; };

var C = loadFrontend(["state.js", "dom.js", "api.js", "shell.js"]);

// The files this driver does not load, as sinks.
C.renderBoardAndSessionsIfChanged = function () {};
C.renderAll = function () {};
C.renderBoard = function () {};
C.refreshDrawerDetail = function () {};   // task-126, in drawer.js

var requests = [];
var boardPayload = { projects: [], version: "v0.1.0-14-g4570911" };
var boardOk = true;
global.fetch = function (url) {
  requests.push(url);
  if (url.indexOf("/api/board") === 0 && !boardOk) {
    return Promise.reject(new Error("network down"));
  }
  var data = url.indexOf("/api/board") === 0 ? boardPayload : {};
  return Promise.resolve({
    ok: true, status: 200,
    json: function () { return Promise.resolve(data); }
  });
};

function credit() {
  var el = document.getElementById("version");
  return { text: el.textContent, title: el.title };
}

var out = {};
out.beforeAnyLoad = credit();

function load() {
  requests = [];
  C.doRefresh(false);
  return settle();
}

load().then(function () {
  out.afterFirstLoad = credit();
  out.requests = requests;
  // A second board load reporting the same value is the normal case
  // (the server resolves it once and repeats it): it must not disturb
  // the node it already settled on.
  document.getElementById("version").title = "TOUCHED";
  return load();
}).then(function () {
  out.afterSecondLoad = credit();
  // A board that carries no version at all -- an older server, or a
  // response that lost the field -- leaves the credit alone rather than
  // writing "undefined" into the footer.
  boardPayload = { projects: [] };
  return load();
}).then(function () {
  out.afterVersionlessLoad = credit();
  boardOk = false;
  return load();
}).then(function () {
  out.afterFailedLoad = credit();
  process.stdout.write(JSON.stringify(out));
}).catch(function (err) {
  process.stderr.write((err && err.stack) || String(err));
  process.exit(1);
});
"""


@js_harness.requires_node
class VersionCreditBehaviourTests(unittest.TestCase):
    """Task-107: what the sidebar footer actually shows, and where it
    got it. The server resolves the version once at startup and repeats
    it on every board response; the frontend's whole job is to display
    that and never to invent one."""

    @property
    def out(self):
        return js_harness.cached_driver(self, VERSION_DRIVER_JS)

    def test_nothing_is_shown_before_the_server_has_answered(self):
        # No placeholder and no client-side default: a number the shell
        # could paint before the first board load would be a second
        # source of truth, and wrong the moment it drifted.
        self.assertEqual(self.out["beforeAnyLoad"]["text"], "")

    def test_it_shows_exactly_what_the_board_response_reported(self):
        self.assertEqual(self.out["afterFirstLoad"]["text"], "v0.1.0-14-g4570911")

    def test_the_tooltip_says_it_is_this_process_and_not_the_checkout(self):
        # The distinction the whole feature exists for: what is running,
        # not what is on disk.
        self.assertEqual(
            self.out["afterFirstLoad"]["title"],
            "Centrale v0.1.0-14-g4570911 \u2014 the build this server process started from")

    def test_the_version_rides_the_board_response_and_costs_no_request(self):
        self.assertEqual(self.out["requests"], ["/api/board", "/api/sessions"])

    def test_a_repeated_value_leaves_the_settled_credit_untouched(self):
        # The driver stamped the title with a sentinel between the two
        # loads: an unchanged value must not rewrite the node.
        self.assertEqual(self.out["afterSecondLoad"]["text"], "v0.1.0-14-g4570911")
        self.assertEqual(self.out["afterSecondLoad"]["title"], "TOUCHED")

    def test_a_response_without_a_version_never_writes_undefined(self):
        self.assertEqual(self.out["afterVersionlessLoad"]["text"], "v0.1.0-14-g4570911")

    def test_a_failed_board_load_leaves_the_last_known_build_in_place(self):
        self.assertEqual(self.out["afterFailedLoad"]["text"], "v0.1.0-14-g4570911")


# ---------------------------------------------------------------------
# The stale-process banner (task-128)
# ---------------------------------------------------------------------

# The other half of the credit above. The server compares the commit it
# loaded with HEAD of its checkout on every board request and reports
# `codeDrift`; what the banner shows is driven through the same real
# fetch -> render path, so a client that decided staleness on its own,
# remembered an earlier answer, or wrote "undefined" into the banner
# would show up here.
CODE_DRIFT_DRIVER_JS = js_harness.LOAD_SOURCES_JS + r"""
global.setInterval = function (fn, ms) { return { fn: fn, ms: ms }; };

var C = loadFrontend(["state.js", "dom.js", "api.js", "shell.js"]);

C.renderBoardAndSessionsIfChanged = function () {};
C.renderAll = function () {};
C.renderBoard = function () {};
C.refreshDrawerDetail = function () {};

var boardPayload = { projects: [], version: "v0.1.0 (055ff59)", codeDrift: null };
var boardOk = true;
global.fetch = function (url) {
  if (url.indexOf("/api/board") === 0 && !boardOk) {
    return Promise.reject(new Error("network down"));
  }
  var data = url.indexOf("/api/board") === 0 ? boardPayload : {};
  return Promise.resolve({
    ok: true, status: 200,
    json: function () { return Promise.resolve(data); }
  });
};

function banner() {
  var el = document.getElementById("code-drift-banner");
  var text = document.getElementById("code-drift-text");
  return { hidden: !!el.hidden, text: text ? text.textContent : "" };
}

var out = {};
out.beforeAnyLoad = banner();

function load(payload) {
  if (payload !== undefined) boardPayload = payload;
  C.doRefresh(false);
  return settle();
}

load().then(function () {
  // A server on its checkout's HEAD: the normal case, and quiet.
  out.afterQuietLoad = banner();
  // The checkout moves under the process: the very next board response
  // says so and the banner appears without the browser doing anything.
  return load({ projects: [], version: "v0.1.0 (055ff59)",
                codeDrift: { loaded: "055ff59", current: "a088d15", commitsBehind: 23 } });
}).then(function () {
  out.afterDrift = banner();
  return load({ projects: [], version: "v0.1.0 (055ff59)",
                codeDrift: { loaded: "055ff59", current: "1a81dd5", commitsBehind: 1 } });
}).then(function () {
  out.afterOneCommit = banner();
  // The count is optional: git could not count, the two commits still differ.
  return load({ projects: [], version: "v0.1.0 (055ff59)",
                codeDrift: { loaded: "055ff59", current: "1a81dd5", commitsBehind: null } });
}).then(function () {
  out.afterUncounted = banner();
  // A failed load leaves the last answer standing rather than clearing
  // a warning that is still true.
  boardOk = false;
  return load();
}).then(function () {
  out.afterFailedLoad = banner();
  boardOk = true;
  // The restart happened (or the checkout came back): quiet again.
  return load({ projects: [], version: "v0.1.0-24-g1a81dd5", codeDrift: null });
}).then(function () {
  out.afterRestart = banner();
  // An older server that reports no field at all: nothing to show.
  return load({ projects: [], version: "v0.1.0 (055ff59)" });
}).then(function () {
  out.afterFieldlessLoad = banner();
  // Garbage in the field is not a signal either.
  return load({ projects: [], version: "v0.1.0 (055ff59)", codeDrift: { loaded: "", current: "x" } });
}).then(function () {
  out.afterGarbage = banner();
  process.stdout.write(JSON.stringify(out));
}).catch(function (err) {
  process.stderr.write((err && err.stack) || String(err));
  process.exit(1);
});
"""


@js_harness.requires_node
class CodeDriftBannerBehaviourTests(unittest.TestCase):
    """Task-128: the banner that says the code on disk has changed since
    this server process started, driven by what the board response
    reports and by nothing else."""

    @property
    def out(self):
        return js_harness.cached_driver(self, CODE_DRIFT_DRIVER_JS)

    def test_nothing_is_written_before_the_server_has_answered(self):
        # The markup ships the banner `hidden` (the shim reads index.html
        # for ids only, not attributes, so that half is a source-shape
        # fact); what this tier can say is that no client-side default
        # is painted into it before the first response.
        self.assertEqual(self.out["beforeAnyLoad"]["text"], "")

    def test_a_server_on_its_checkouts_head_shows_nothing(self):
        self.assertTrue(self.out["afterQuietLoad"]["hidden"])
        self.assertEqual(self.out["afterQuietLoad"]["text"], "")

    def test_a_moved_checkout_shows_both_commits_the_count_and_what_to_do(self):
        shown = self.out["afterDrift"]
        self.assertFalse(shown["hidden"])
        self.assertEqual(
            shown["text"],
            "The code on disk has changed since this server process started: it loaded "
            "055ff59, the checkout is now at a088d15 (23 commits later). Restart Centrale "
            "to load it \u2014 after checking no merge is in flight.")

    def test_one_commit_reads_singular_and_an_uncounted_drift_omits_the_count(self):
        self.assertIn("(1 commit later)", self.out["afterOneCommit"]["text"])
        uncounted = self.out["afterUncounted"]["text"]
        self.assertFalse(uncounted.endswith(")"), uncounted)
        self.assertNotIn("commit", uncounted.split("now at 1a81dd5")[1].split(".")[0])
        self.assertIn("now at 1a81dd5. Restart Centrale", uncounted)

    def test_a_failed_load_leaves_a_true_warning_standing(self):
        self.assertFalse(self.out["afterFailedLoad"]["hidden"])
        self.assertIn("now at 1a81dd5", self.out["afterFailedLoad"]["text"])

    def test_a_restart_clears_it_on_the_next_response(self):
        self.assertTrue(self.out["afterRestart"]["hidden"])

    def test_an_older_server_without_the_field_shows_nothing(self):
        self.assertTrue(self.out["afterFieldlessLoad"]["hidden"])

    def test_a_malformed_field_is_not_a_signal(self):
        self.assertTrue(self.out["afterGarbage"]["hidden"])


# ---------------------------------------------------------------------
# task-120: the dead end a server restart used to leave behind
# ---------------------------------------------------------------------

# Two rules that were each defensible alone met on one task and left it
# with nothing to click: End session was offered only for an allowlist
# of agent states ("unknown" not among them), and the harvest area
# rendered nothing at all while a session was live. A server restart
# clears the in-memory event store, and an idle agent emits nothing to
# rebuild it from, so `GET /api/sessions` reports every still-running
# session as `agentState: "unknown"` -- which is exactly how this was
# hit in real use.
#
# This drives the restart itself rather than describing it: the board
# and the sessions payload are the ones a freshly restarted server
# answers with (a live tmux session, no lifecycle event anywhere), and
# the drawer, the sidebar panel and the harvest area are then read back
# as a viewer would see them, along with what each button reaches.
NO_WAY_OUT_DRIVER_JS = js_harness.LOAD_SOURCES_JS + r"""
var C = loadFrontend(["state.js", "dom.js", "tasks.js", "board.js",
                      "spawn.js", "harvest.js", "sessions.js", "drawer.js"]);

// The files this driver does not load, as inert sinks.
C.syncDrawerPanePolling = function () {};
C.renderProjectChips = function () {};
C.renderErrorBanners = function () {};
C.doRefresh = function () {};
C.fetchSessions = function () {};
C.showToast = function () {};
C.copyText = function () {};
C.openDrawer = function () {};

var requests = [];
global.fetch = function (url, opts) {
  requests.push(((opts && opts.method) || "GET") + " " + url.split("?")[0]);
  return Promise.resolve({ ok: true, status: 200,
    json: function () { return Promise.resolve({ ended: true }); } });
};

var SESSION = "centrale-my-tool-task-1";
// The task as the board reports it after a restart: an agent has been
// running in its worktree since before the restart, so there is a
// branch and a dirty worktree, and main's copy still says In Progress.
function task() {
  return {
    id: "TASK-1", title: "A task", status: "In Progress", ready: true,
    hasSpawnBranch: true, alreadyMerged: false, worktreeDirty: true,
    branchCheckout: { kind: "centrale", path: "/w/my-tool-task-1" }
  };
}

// `live` is the agentState /api/sessions reports for the one live tmux
// session, or null for no session at all. `undefined` is the third
// case: a payload with no agentState key whatsoever.
function render(live) {
  var t = task();
  C.knownProjects.length = 0;
  C.knownProjects.push("my-tool");
  C.activeProjects = new Set(["my-tool"]);
  C.boardData = { projects: [{ name: "my-tool", tasks: [t],
                               statuses: ["To Do", "In Progress", "Done"] }],
                  capabilities: { tmux: true } };
  C.sessionsData = live === null ? [] : [(function () {
    var row = { name: SESSION, project: "my-tool" };
    if (live !== undefined) row.agentState = live;
    return row;
  })()];
  C.firstLoadDone = true;
  [C.harvestStates, C.spawnStates, C.cleanupStates, C.endSessionStates,
   C.discardStates, C.abandonStates, C.endSessionConfirmPending].forEach(function (map) {
    Object.keys(map).forEach(function (k) { delete map[k]; });
  });
  C.currentDrawer = { project: "my-tool", id: "TASK-1", summary: t, branchTask: null };
  C.renderDrawerSessionArea();
  C.renderDrawerHarvestArea();
  C.renderSessionsPanel();
  return snapshot();
}

function snapshot() {
  return {
    session: read("drawer-session-area"),
    harvest: read("drawer-harvest-area"),
    panel: read("sessions-list")
  };
}

function read(id) {
  var el = C.byId(id);
  return {
    buttons: el.querySelectorAll("button").map(function (b) {
      return { text: b.textContent, disabled: !!b.disabled, title: b.title || "" };
    }),
    lines: (function () {
      var found = [];
      (function walk(n) {
        if (String(n.className || "").indexOf("spawn-status-line") === 0) found.push(n.textContent);
        else n.childNodes.forEach(walk);
      })(el);
      return found;
    })()
  };
}

// Click every button carrying `text` anywhere in `id`, and report what
// reached the server. A disabled control that still had a handler would
// show up here as a request.
function clickAll(id, text) {
  requests.length = 0;
  C.byId(id).querySelectorAll("button").forEach(function (b) {
    if (b.textContent === text) b.click();
  });
  return requests.slice();
}

var out = {};

// -- the restart itself: a live session, an empty event store --
out.restart = render("unknown");
out.restartEndSessionClick = clickAll("drawer-session-area", "End session");
render("unknown");
out.restartPanelEndSessionClick = clickAll("sessions-list", "End session");

// -- the same payload with no agentState key at all --
out.noStateKey = render(undefined);

// -- a working agent: clickable, and the first click ARMS (task-132) --
out.working = render("working");
out.workingEndSessionClick = clickAll("drawer-session-area", "End session");
out.workingAfterDrawerClick = snapshot();
render("working");
out.workingPanelEndSessionClick = clickAll("sessions-list", "End session");
out.workingAfterPanelClick = snapshot();

// -- the states that were always offered, unchanged --
out.waiting = render("waiting");
out.finished = render("finished");

// -- and no session at all: nothing to end, and the real actions back --
out.noSession = render(null);

require("fs").writeSync(1, JSON.stringify(out));
process.exit(0);
"""


@js_harness.requires_node
class NoWayOutSessionBehaviourTests(unittest.TestCase):
    """task-120: every live session offers a way out, and every action
    Centrale withholds says why.

    The bug was the absence of controls, which no source-text test can
    see: both halves of it were correct code (an allowlist that omitted
    "unknown", an early `return` on a live session), and what was wrong
    was the page they added up to. So this reads the rendered drawer,
    the rendered sidebar panel and what each button reaches.
    """

    @property
    def out(self):
        return js_harness.cached_driver(self, NO_WAY_OUT_DRIVER_JS)

    END = "End session"

    def _named(self, area, text):
        return [b for b in area["buttons"] if b["text"] == text]

    # -- AC #4: the restart-induced case --

    def test_a_restart_stranded_session_still_offers_a_way_out(self):
        # A live tmux session and an event store with nothing in it --
        # the state a single server restart leaves every running session
        # in. Both surfaces offer End session, and both mean it.
        for surface in ("session", "panel"):
            with self.subTest(surface):
                btns = self._named(self.out["restart"][surface], self.END)
                self.assertEqual(len(btns), 1)
                self.assertIs(btns[0]["disabled"], False)
        self.assertEqual(self.out["restartEndSessionClick"], ["POST /api/end-session"])
        self.assertEqual(self.out["restartPanelEndSessionClick"], ["POST /api/end-session"])

    def test_a_sessions_payload_with_no_agent_state_at_all_is_the_same_case(self):
        # `agentState` missing entirely, rather than the string
        # "unknown": effectiveAgentState normalizes it, and the offer
        # must not depend on which of the two the server sent.
        self.assertEqual([b["text"] for b in self.out["noStateKey"]["session"]["buttons"]],
                         [self.END])
        self.assertIs(self.out["noStateKey"]["session"]["buttons"][0]["disabled"], False)

    def test_the_stranded_task_is_not_left_without_any_action_at_all(self):
        # The whole point, stated as the viewer's question: is there
        # anything to click? Before this, the drawer of a task with a
        # branch and a dirty worktree rendered one badge and nothing else.
        restart = self.out["restart"]
        clickable = [b for area in ("session", "harvest")
                     for b in restart[area]["buttons"] if not b["disabled"]]
        self.assertEqual([b["text"] for b in clickable], [self.END])

    # -- task-132: a working agent's button is clickable, and its first
    #    click arms rather than ends (task-120's AC #2 rendered it
    #    disabled with the reason, which took the button away in the
    #    one state a user actually reaches for it) --

    ARMED = "Agent is mid-turn — end anyway?"

    def test_a_working_agent_keeps_the_button_and_it_is_clickable(self):
        for surface in ("session", "panel"):
            with self.subTest(surface):
                btns = self._named(self.out["working"][surface], self.END)
                self.assertEqual(len(btns), 1)
                self.assertIs(btns[0]["disabled"], False)
        # Nothing under the resting button: the reason belongs to the
        # armed state, not to a button nobody has clicked yet.
        self.assertEqual(self.out["working"]["session"]["lines"], [])

    def test_the_first_click_on_a_working_agent_arms_and_does_not_reach_the_server(self):
        # The server has no agent-state gate of its own, so the
        # protection lives entirely in this first click not being the
        # one that POSTs. From either surface.
        self.assertEqual(self.out["workingEndSessionClick"], [])
        self.assertEqual(self.out["workingPanelEndSessionClick"], [])
        for after in ("workingAfterDrawerClick", "workingAfterPanelClick"):
            for surface in ("session", "panel"):
                with self.subTest(after=after, surface=surface):
                    btns = self._named(self.out[after][surface], self.ARMED)
                    self.assertEqual(len(btns), 1, self.out[after][surface]["buttons"])
                    self.assertIs(btns[0]["disabled"], False)
                    self.assertIn("mid-turn", btns[0]["title"])
            # The drawer spells the whole reason out under the armed
            # button; the sidebar row has no room and says it in the
            # label instead, which the loop above already read.
            self.assertTrue(any("mid-turn" in line
                                for line in self.out[after]["session"]["lines"]),
                            self.out[after]["session"]["lines"])

    def test_the_states_that_were_always_actionable_are_untouched(self):
        for state in ("waiting", "finished"):
            with self.subTest(state):
                btns = self._named(self.out[state]["session"], self.END)
                self.assertEqual(len(btns), 1)
                self.assertIs(btns[0]["disabled"], False)

    # -- AC #3: the throwaway actions while a session is live --

    def test_the_throwaway_actions_render_disabled_naming_the_session(self):
        harvest = self.out["restart"]["harvest"]
        self.assertEqual([b["text"] for b in harvest["buttons"]],
                         ["Merge", "Abandon worktree, keep branch", "Discard attempt"])
        for btn in harvest["buttons"]:
            with self.subTest(btn["text"]):
                self.assertIs(btn["disabled"], True)
                self.assertIn("centrale-my-tool-task-1", btn["title"])
                self.assertIn("End the session first", btn["title"])
        self.assertEqual(len(harvest["lines"]), 1)
        self.assertIn("centrale-my-tool-task-1", harvest["lines"][0])

    def test_ending_the_session_is_what_hands_the_actions_back(self):
        # Same task, same branch, no session: the disabled trio becomes
        # the real one -- and gains the two spawn actions, which a live
        # session has nothing to offer (task-125 put all four secondary
        # actions in one row; a live session's copy of that row holds
        # only the two that would still mean something).
        harvest = self.out["noSession"]["harvest"]
        self.assertEqual([b["text"] for b in harvest["buttons"]],
                         ["Merge", "Resume agent", "Re-spawn agent",
                          "Abandon worktree, keep branch", "Discard attempt"])
        for btn in harvest["buttons"]:
            self.assertIs(btn["disabled"], False, btn["text"])

    # -- AC #5: nothing silently absent --

    def test_no_session_means_no_end_session_button_which_is_honest(self):
        # The one absence that stays: with no live session there is
        # nothing to end, and the drawer's session area says nothing at
        # all rather than offering a control that could only 404.
        self.assertEqual(self.out["noSession"]["session"]["buttons"], [])
        self.assertEqual(self.out["noSession"]["panel"]["buttons"], [])

    def test_every_live_session_renders_the_button_in_both_surfaces(self):
        # The invariant behind AC #5, over every state this driver
        # rendered with a live session: the button is always there --
        # and, since task-132, never disabled for the agent's state.
        for case in ("restart", "noStateKey", "working", "waiting", "finished"):
            for surface in ("session", "panel"):
                with self.subTest(case=case, surface=surface):
                    btns = self._named(self.out[case][surface], self.END)
                    self.assertEqual(len(btns), 1)
                    self.assertIs(btns[0]["disabled"], False)


# ---------------------------------------------------------------------
# task-132: End session on a working agent is a two-click arm
# ---------------------------------------------------------------------

# task-120 left End session DISABLED for the whole time an agent's last
# event was "working" -- which a fresh spawn reports within seconds and
# a long task stays in for an hour, and which is the one state in which
# a user actually needs the button (a spawn by mistake, the wrong task,
# an agent looping or stuck in a long tool call). The protection against
# one stray click moves to the two-click arm Merge, cleanup and Discard
# already use.
#
# The clock and the timers are the driver's, the PANE_DRIVER_JS way:
# `Date.now` is frozen and advanced by hand, and `setTimeout` records
# what was armed rather than waiting, so "the window expired" is a
# thing this driver DOES, and what the expiry then rendered and sent is
# read back rather than assumed.
END_SESSION_ARM_DRIVER_JS = js_harness.LOAD_SOURCES_JS + r"""
var C = loadFrontend(["state.js", "dom.js", "tasks.js", "board.js",
                      "spawn.js", "harvest.js", "sessions.js", "drawer.js"]);

C.syncDrawerPanePolling = function () {};
C.renderProjectChips = function () {};
C.renderErrorBanners = function () {};
C.doRefresh = function () {};
C.fetchSessions = function () {};
C.showToast = function () {};
C.copyText = function () {};
C.openDrawer = function () {};

// -- a clock the driver owns --
var NOW = 1767225600000;
Date.now = function () { return NOW; };
function advance(ms) { NOW += ms; }

// -- timers the driver owns: armed, never waited on --
var timers = [];
global.setTimeout = function (fn, ms) {
  var t = { fn: fn, ms: ms, dead: false };
  timers.push(t);
  return t;
};
global.clearTimeout = function (t) { if (t) t.dead = true; };
function fireDueTimers() {
  timers.filter(function (t) { return !t.dead; }).forEach(function (t) {
    t.dead = true;
    t.fn();
  });
}

var requests = [];
global.fetch = function (url, opts) {
  requests.push(((opts && opts.method) || "GET") + " " + url.split("?")[0]);
  if (opts && opts.body) requests[requests.length - 1] += " " + opts.body;
  return Promise.resolve({ ok: true, status: 200,
    json: function () { return Promise.resolve({ ended: true }); } });
};

var SESSION = "centrale-my-tool-task-1";
function task() {
  return {
    id: "TASK-1", title: "A task", status: "In Progress", ready: true,
    hasSpawnBranch: true, alreadyMerged: false, worktreeDirty: true,
    branchCheckout: { kind: "centrale", path: "/w/my-tool-task-1" }
  };
}

// A live session in `agentState`, the drawer open on its task, both
// surfaces rendered. Every per-task state map is cleared, so each
// scenario below starts from a resting button.
function render(agentState) {
  var t = task();
  C.knownProjects.length = 0;
  C.knownProjects.push("my-tool");
  C.activeProjects = new Set(["my-tool"]);
  C.boardData = { projects: [{ name: "my-tool", tasks: [t],
                               statuses: ["To Do", "In Progress", "Done"] }],
                  capabilities: { tmux: true } };
  C.sessionsData = [{ name: SESSION, project: "my-tool", agentState: agentState }];
  C.firstLoadDone = true;
  [C.harvestStates, C.spawnStates, C.cleanupStates, C.endSessionStates,
   C.discardStates, C.abandonStates, C.endSessionConfirmPending].forEach(function (map) {
    Object.keys(map).forEach(function (k) { delete map[k]; });
  });
  timers.length = 0;
  requests.length = 0;
  C.currentDrawer = { project: "my-tool", id: "TASK-1", summary: t, branchTask: null };
  C.renderDrawerSessionArea();
  C.renderSessionsPanel();
}

// The one End session control in `id` -- whatever it is labelled --
// as a viewer sees it: resting, armed, or in flight.
function control(id) {
  var btns = C.byId(id).querySelectorAll("button").filter(function (b) {
    return /end/i.test(b.textContent);
  });
  if (btns.length !== 1) throw new Error(id + " has " + btns.length + " End controls");
  var b = btns[0];
  return { text: b.textContent, disabled: !!b.disabled, title: b.title || "",
           confirming: String(b.className).split(/\s+/).indexOf("confirming") !== -1 };
}
function lines(id) {
  var found = [];
  (function walk(n) {
    if (String(n.className || "").indexOf("spawn-status-line") === 0) found.push(n.textContent);
    else n.childNodes.forEach(walk);
  })(C.byId(id));
  return found;
}
function both() {
  return { drawer: control("drawer-session-area"), panel: control("sessions-list"),
           drawerLines: lines("drawer-session-area") };
}
function clickIn(id) {
  var before = requests.length;
  C.byId(id).querySelectorAll("button").forEach(function (b) {
    if (/end/i.test(b.textContent)) b.click();
  });
  return requests.slice(before);
}

var out = {};

function walk() {
  // -- 1. working: arm from the sidebar, end from the drawer --
  render("working");
  out.working = {};
  out.working.resting = both();
  out.working.firstClickSent = clickIn("sessions-list");
  out.working.armed = both();
  out.working.armedTimerDelays = timers.map(function (t) { return t.ms; });
  advance(C.SPAWN_CONFIRM_WINDOW_MS - 1000);
  out.working.secondClickSent = clickIn("drawer-session-area");
  out.working.inFlight = both();
  return settle().then(function () {
    out.working.allSent = requests.slice();

    // -- 2. working: arm, then let the window expire --
    render("working");
    out.expiry = {};
    out.expiry.firstClickSent = clickIn("drawer-session-area");
    out.expiry.armed = both();
    advance(C.SPAWN_CONFIRM_WINDOW_MS + 100);
    fireDueTimers();
    out.expiry.afterExpiry = both();
    out.expiry.sentInAll = requests.slice();
    // A click after the expiry is a FIRST click again.
    out.expiry.clickAfterExpirySent = clickIn("drawer-session-area");
    out.expiry.reArmed = both();
    return settle();
  }).then(function () {
    // -- 3. the states that end on one click --
    out.oneClick = {};
    var states = ["waiting", "finished", "idle", "unknown"];
    return states.reduce(function (chain, state) {
      return chain.then(function () {
        render(state);
        var resting = both().panel;
        var sent = clickIn("sessions-list");
        out.oneClick[state] = { resting: resting, sent: sent };
        return settle();
      });
    }, Promise.resolve());
  }).then(function () {
    // -- 4. the arm is shared: arm in the drawer, read the sidebar --
    render("working");
    clickIn("drawer-session-area");
    out.sharedFromDrawer = both();
    // ...and the second click can land on the OTHER surface.
    out.sharedSecondClickSent = clickIn("sessions-list");
    return settle();
  }).then(function () {
    require("fs").writeSync(1, JSON.stringify(out));
    process.exit(0);
  });
}

walk().catch(function (err) {
  require("fs").writeSync(2, String(err && err.stack || err));
  process.exit(1);
});
"""


@js_harness.requires_node
class EndSessionArmBehaviourTests(unittest.TestCase):
    """task-132: End session is always clickable; a working agent takes
    two clicks, any other state takes one, and the drawer's button and
    the sidebar row's are one control in two places."""

    @property
    def out(self):
        return js_harness.cached_driver(self, END_SESSION_ARM_DRIVER_JS)

    END = "End session"
    ARMED = "Agent is mid-turn — end anyway?"
    POST = "POST /api/end-session"

    # -- AC #1: never disabled for the agent's state --

    def test_the_resting_button_on_a_working_agent_is_enabled_in_both_places(self):
        resting = self.out["working"]["resting"]
        for surface in ("drawer", "panel"):
            with self.subTest(surface):
                self.assertEqual(resting[surface]["text"], self.END)
                self.assertIs(resting[surface]["disabled"], False)
                self.assertIs(resting[surface]["confirming"], False)

    def test_the_only_disabled_rendering_is_the_in_flight_one(self):
        # Once the second click has sent the POST, both surfaces show
        # the in-flight label, disabled -- the one disabled state left.
        in_flight = self.out["working"]["inFlight"]
        for surface in ("drawer", "panel"):
            with self.subTest(surface):
                self.assertEqual(in_flight[surface]["text"], "Ending…")
                self.assertIs(in_flight[surface]["disabled"], True)

    # -- AC #2 / #6: the first click arms, the label says why, the second ends --

    def test_the_first_click_arms_with_the_reason_in_the_label_and_sends_nothing(self):
        working = self.out["working"]
        self.assertEqual(working["firstClickSent"], [])
        for surface in ("drawer", "panel"):
            with self.subTest(surface):
                armed = working["armed"][surface]
                self.assertEqual(armed["text"], self.ARMED)
                self.assertIs(armed["disabled"], False)
                self.assertIs(armed["confirming"], True)
                self.assertIn("mid-turn", armed["text"])
                self.assertIn("end", armed["text"].lower())
                self.assertIn("Click again to end it anyway", armed["title"])
        # The drawer also spells the whole sentence out under the button.
        self.assertEqual(len(working["armed"]["drawerLines"]), 1)
        self.assertIn("mid-turn", working["armed"]["drawerLines"][0])
        # ...and only while armed: nothing under the resting one.
        self.assertEqual(working["resting"]["drawerLines"], [])

    def test_the_second_click_within_the_window_ends_the_session(self):
        # AC #7: the POST is the one endSession has always sent, with
        # the same body -- the arm changes when it is sent, not what.
        working = self.out["working"]
        self.assertEqual(working["secondClickSent"],
                         [self.POST + ' {"project":"my-tool","taskId":"TASK-1"}'])
        self.assertEqual(len([r for r in working["allSent"] if r.startswith(self.POST)]), 1)

    # -- AC #3: the arm expires with no action --

    def test_the_arm_uses_the_same_window_as_the_other_armed_actions(self):
        # One expiry timer, at the window every other confirm uses.
        self.assertEqual(self.out["working"]["armedTimerDelays"], [5000 + 100])

    def test_an_expired_arm_disarms_without_a_post_and_restores_the_resting_label(self):
        expiry = self.out["expiry"]
        self.assertEqual(expiry["firstClickSent"], [])
        self.assertEqual(expiry["armed"]["drawer"]["text"], self.ARMED)
        for surface in ("drawer", "panel"):
            with self.subTest(surface):
                after = expiry["afterExpiry"][surface]
                self.assertEqual(after["text"], self.END)
                self.assertIs(after["disabled"], False)
                self.assertIs(after["confirming"], False)
        self.assertEqual(expiry["afterExpiry"]["drawerLines"], [])
        self.assertEqual(expiry["sentInAll"], [])

    def test_a_click_after_the_expiry_is_a_first_click_again(self):
        expiry = self.out["expiry"]
        self.assertEqual(expiry["clickAfterExpirySent"], [])
        self.assertEqual(expiry["reArmed"]["panel"]["text"], self.ARMED)

    # -- AC #4: every other state ends on one click --

    def test_every_non_working_state_ends_on_one_click(self):
        for state in ("waiting", "finished", "idle", "unknown"):
            with self.subTest(state):
                case = self.out["oneClick"][state]
                self.assertEqual(case["resting"]["text"], self.END)
                self.assertIs(case["resting"]["disabled"], False)
                self.assertEqual(case["sent"],
                                 [self.POST + ' {"project":"my-tool","taskId":"TASK-1"}'])

    # -- AC #5: one arm, two surfaces --

    def test_arming_in_the_drawer_shows_armed_in_the_sidebar_and_vice_versa(self):
        # Drawer-first here; the sidebar-first direction is the flow
        # test_the_first_click_arms... read, which armed from the panel
        # and found the drawer armed.
        shared = self.out["sharedFromDrawer"]
        self.assertEqual(shared["drawer"]["text"], self.ARMED)
        self.assertEqual(shared["panel"]["text"], self.ARMED)
        self.assertEqual(self.out["working"]["armed"]["drawer"]["text"], self.ARMED)

    def test_the_second_click_may_land_on_the_other_surface(self):
        self.assertEqual(self.out["sharedSecondClickSent"],
                         [self.POST + ' {"project":"my-tool","taskId":"TASK-1"}'])


# ---------------------------------------------------------------------
# The tier's own runtime gate (task-124)
# ---------------------------------------------------------------------


class BehaviouralTierRuntimeGateTests(unittest.TestCase):
    """What a missing `node` does to this tier -- skip, or fail.

    Everything else in this module needs node to say anything. These
    tests are about the decision one level up, so they run everywhere:
    on a development machine an absent runtime is a clean skip (and
    `discover tests` still reports OK), while a caller that sets
    `CENTRALE_REQUIRE_NODE` -- scripts/release.sh's gate is the only one
    -- gets a failure instead. Nothing about that is visible from a
    passing suite, which is precisely how a syntax-broken frontend used
    to reach a release.
    """

    def _result_of(self, case):
        result = unittest.TestResult()
        unittest.defaultTestLoader.loadTestsFromTestCase(case).run(result)
        return result

    def _probe(self, decorate_class):
        """A one-test TestCase behind `requires_node`, decorated as a
        class or as a method -- both spellings are in use across this
        module -- that records whether its body ever ran."""
        ran = []

        class Probe(unittest.TestCase):
            def test_body(self):
                ran.append(True)

        if decorate_class:
            Probe = js_harness.requires_node(Probe)
        else:
            Probe.test_body = js_harness.requires_node(Probe.test_body)
        return Probe, ran

    def _without_node(self, required):
        """Context: no `node` on PATH, and the switch set or unset."""
        env = mock.patch.dict(os.environ, {js_harness.REQUIRE_NODE_ENV: "1"}) \
            if required else mock.patch.dict(
                os.environ, {}, clear=False)
        no_node = mock.patch.object(js_harness, "node_path", return_value=None)
        return env, no_node

    def test_a_missing_node_is_a_clean_skip_in_ordinary_development(self):
        env, no_node = self._without_node(required=False)
        with env, no_node:
            os.environ.pop(js_harness.REQUIRE_NODE_ENV, None)
            for decorate_class in (False, True):
                with self.subTest(decorate_class=decorate_class):
                    probe, ran = self._probe(decorate_class)
                    result = self._result_of(probe)

                    self.assertEqual(len(result.skipped), 1, result.skipped)
                    self.assertEqual(result.failures, [])
                    self.assertEqual(result.errors, [])
                    self.assertEqual(ran, [], "the body must not have run")

    def test_the_switch_turns_that_skip_into_a_failure(self):
        env, no_node = self._without_node(required=True)
        with env, no_node:
            for decorate_class in (False, True):
                with self.subTest(decorate_class=decorate_class):
                    probe, ran = self._probe(decorate_class)
                    result = self._result_of(probe)

                    # A FAILURE, not a skip and not an error: the tier
                    # could not run, and a release must hear about it.
                    self.assertEqual(result.skipped, [])
                    self.assertEqual(result.errors, [])
                    self.assertEqual(len(result.failures), 1)
                    message = result.failures[0][1]
                    self.assertIn(js_harness.REQUIRE_NODE_ENV, message)
                    self.assertIn("node is not on PATH", message)
                    self.assertEqual(ran, [], "the body must not have run")

    def test_a_machine_with_node_is_untouched_either_way(self):
        for required in (False, True):
            with self.subTest(required=required):
                env = mock.patch.dict(
                    os.environ, {js_harness.REQUIRE_NODE_ENV: "1"} if required else {})
                with env, mock.patch.object(
                        js_harness, "node_path", return_value="/usr/bin/node"):
                    if not required:
                        os.environ.pop(js_harness.REQUIRE_NODE_ENV, None)
                    probe, ran = self._probe(decorate_class=False)
                    result = self._result_of(probe)

                    self.assertEqual(result.skipped, [])
                    self.assertEqual(result.failures, [])
                    self.assertEqual(result.errors, [])
                    self.assertEqual(ran, [True])

    def test_only_a_meaningful_value_arms_the_switch(self):
        for value, expected in (("1", True), ("yes", True), (" ", False),
                                ("", False), ("0", False)):
            with self.subTest(value=value):
                with mock.patch.dict(os.environ,
                                     {js_harness.REQUIRE_NODE_ENV: value}):
                    self.assertEqual(js_harness.node_required(), expected)
        with mock.patch.dict(os.environ, {}):
            os.environ.pop(js_harness.REQUIRE_NODE_ENV, None)
            self.assertFalse(js_harness.node_required())

    def test_the_driver_itself_says_what_is_missing_rather_than_crashing(self):
        # Belt and braces for a path that reaches the harness without
        # the decorator's verdict: `subprocess.run([None, ...])` raises
        # a TypeError that names nothing.
        with mock.patch.object(js_harness, "node_path", return_value=None):
            with self.assertRaises(AssertionError) as caught:
                js_harness.run_driver(self, "console.log('{}')")
        self.assertIn(js_harness.REQUIRE_NODE_ENV, str(caught.exception))

    def test_only_the_release_gate_arms_the_switch(self):
        # The switch is a release's, and only a release's: a stray
        # export anywhere in the executable tree would make every
        # developer's suite fail on a machine without node, which is the
        # behaviour task-124 deliberately kept. (Prose that names the
        # variable is not a setter, so the docs are not read here.)
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        setters = []
        for where in (root, os.path.join(root, "scripts"),
                      os.path.join(root, "tests"),
                      os.path.join(root, "tests_integration")):
            for name in sorted(os.listdir(where)):
                path = os.path.join(where, name)
                if not os.path.isfile(path) or not name.endswith((".py", ".sh")):
                    continue
                with open(path, "r", encoding="utf-8", errors="replace") as f:
                    for lineno, line in enumerate(f, 1):
                        if js_harness.REQUIRE_NODE_ENV + "=" in line:
                            setters.append("%s:%d" % (path[len(root) + 1:], lineno))
        self.assertTrue(
            any(s.startswith("scripts/release.sh:") for s in setters),
            "the release gate must be the thing that sets it: " + repr(setters))
        stray = [s for s in setters
                 if not s.startswith(("scripts/release.sh:",
                                      "tests/js_harness.py:",
                                      "tests/test_frontend_behaviour.py:",
                                      "tests_integration/test_release_integration.py:"))]
        self.assertEqual(stray, [], "unexpected setter(s) of " + js_harness.REQUIRE_NODE_ENV)


# ---------------------------------------------------------------------
# task-126: the drawer's body, refreshed while it stays open
# ---------------------------------------------------------------------

# The drawer's body used to be fetched exactly once, at open, so an
# acceptance criterion an agent ticked while you were reading it stayed
# unticked until you closed and reopened the drawer. It now rides the
# ordinary refresh tick -- which is a claim about a SEQUENCE of ticks
# over one open drawer, so it is driven here as exactly that: open the
# drawer, then call the real C.doRefresh() (api.js's own entry point,
# not the drawer function it calls) once per scenario and read what
# happened to the body.
#
# "Touched no DOM" is observed rather than inferred: every node in the
# body is stamped before a tick and the stamps are read back after it.
# A rebuild -- which is what re-folds sections, resets scroll and drops
# focus -- loses them all, so an untouched refresh cannot be faked by a
# render that happened to produce equal text.
DRAWER_BODY_REFRESH_DRIVER_JS = js_harness.LOAD_SOURCES_JS + r"""
// Every timer this flow arms, recorded rather than run: api.js arms the
// refresh countdown at load (so nothing real is scheduled and node's
// loop is never held open), and a body refresh that grew a poller of
// its own -- a second cadence beside the pane's one poller -- would
// show up here as an entry nothing else accounts for.
var timers = [];
global.setInterval = function (fn, ms) { timers.push({ kind: "interval", ms: ms }); return {}; };
global.setTimeout = function (fn, ms) { timers.push({ kind: "timeout", ms: ms }); return {}; };
global.clearTimeout = function () {};

var C = loadFrontend(["state.js", "dom.js", "api.js", "tasks.js", "board.js",
                      "spawn.js", "harvest.js", "drawer.js", "pane.js"]);

// The files this driver does not load, as inert sinks.
C.renderProjectChips = function () {};
C.renderErrorBanners = function () {};
C.renderMilestoneFilter = function () {};
C.renderMilestoneChipLabel = function () {};
C.renderSessionsPanel = function () {};
C.renderBoard = function () {};
C.renderBoardAndSessionsIfChanged = function () {};
C.renderVersion = function () {};
C.showToast = function () {};
C.copyText = function () {};
wireDrawerShell();

var STATUSES = ["To Do", "In Progress", "Done"];
function boardTask(id) {
  return { id: id, title: "A task", status: "In Progress", ready: true,
           hasSpawnBranch: true, alreadyMerged: false, worktreeDirty: false,
           branchCheckout: { kind: "centrale" } };
}
var PROJECT = { name: "my-tool", statuses: STATUSES,
                tasks: [boardTask("TASK-126"), boardTask("TASK-99")] };
var BOARD = { projects: [PROJECT], capabilities: { tmux: true } };
C.knownProjects.length = 0;
C.knownProjects.push("my-tool");
C.activeProjects = new Set(["my-tool"]);
C.boardData = BOARD;
C.sessionsData = [];
C.firstLoadDone = true;

// What `GET /api/task` would answer for each task, as the disk has it
// right now -- the driver edits these between ticks the way an agent
// edits the task file between ticks.
var VIEWS = {};
function setView(id, opts) {
  opts = opts || {};
  VIEWS[id] = {
    task: { id: id, title: "A task", status: "In Progress",
            description: "A short description.",
            acceptanceCriteria: [
              { text: "the body follows the file", checked: !!opts.checked },
              { text: "an unchanged tick touches nothing", checked: false }
            ],
            dependencies: [],
            implementationNotes: opts.notes || "" },
    branchTask: { task: { id: id, status: opts.branchStatus || "In Progress",
                          acceptanceCriteria: [
                            { text: "the body follows the file", checked: !!opts.checked },
                            { text: "an unchanged tick touches nothing", checked: false }
                          ] } }
  };
}
setView("TASK-126");
setView("TASK-99");

var requests = [];
var TASK_FETCH_FAILS = false;
var HOLD = false;      // hold the next /api/task response mid-flight
var release = null;    // ...and this lets it land
global.fetch = function (url, opts) {
  requests.push(((opts && opts.method) || "GET") + " " + url);
  url = String(url);
  function answer(data) {
    // A response is its own object, as it would be over the wire: a
    // body that compared identity rather than content would pass here
    // for the wrong reason.
    var copy = JSON.parse(JSON.stringify(data));
    return { ok: true, status: 200, json: function () { return Promise.resolve(copy); } };
  }
  if (url.indexOf("/api/task") === 0) {
    if (TASK_FETCH_FAILS) return Promise.reject(new Error("network down"));
    var id = decodeURIComponent(url.split("&id=")[1]);
    var payload = VIEWS[id];
    if (HOLD) {
      return new Promise(function (resolve) {
        release = function () { resolve(answer(payload)); };
      });
    }
    return Promise.resolve(answer(payload));
  }
  if (url.indexOf("/api/board") === 0) return Promise.resolve(answer(BOARD));
  return Promise.resolve(answer({ sessions: [] }));
};

// -- reading the body back --

function body() { return C.byId("drawer-body"); }
function bodyText() { return body().textContent; }
function taskRequests() {
  return requests.filter(function (r) { return r.indexOf("GET /api/task") === 0; });
}
function sections() {
  return body().childNodes
    .filter(function (n) { return n.attrs && n.attrs["data-section"]; })
    .map(function (s) {
      return { key: s.attrs["data-section"],
               collapsed: String(s.className).split(/\s+/).indexOf("collapsed") !== -1,
               header: s.childNodes[0].textContent };
    });
}
function sectionNode(key) {
  return body().childNodes.filter(function (n) {
    return n.attrs && n.attrs["data-section"] === key;
  })[0] || null;
}
function ticks(key) {
  var out = [];
  var node = sectionNode(key);
  if (node) (function walk(n) {
    if (String(n.tagName).toLowerCase() === "input") out.push(!!n.checked);
    n.childNodes.forEach(walk);
  })(node);
  return out;
}
function firstBox(key) {
  var found = null;
  var node = sectionNode(key);
  if (node) (function walk(n) {
    if (!found && String(n.tagName).toLowerCase() === "input") found = n;
    n.childNodes.forEach(walk);
  })(node);
  return found;
}
function toggleSection(key) {
  sectionNode(key).childNodes[0].childNodes[0].click();
}

// Every node in the body, stamped -- see the comment above the driver.
var stampSeq = 0;
function stamp() {
  stampSeq++;
  (function walk(n) { n.__stamp = stampSeq; n.childNodes.forEach(walk); })(body());
}
function stampsIntact() {
  var intact = true, nodes = 0;
  (function walk(n) {
    nodes++;
    if (n.__stamp !== stampSeq) intact = false;
    n.childNodes.forEach(walk);
  })(body());
  return { intact: intact, nodes: nodes };
}
function reachable(target) {
  var hit = false;
  (function walk(n) { if (n === target) hit = true; n.childNodes.forEach(walk); })(body());
  return hit;
}

var out = {};

// 1. No drawer open: a refresh tick asks for no task body at all.
requests.length = 0;
C.doRefresh(false);
settle().then(function () {
  out.closedTick = { taskRequests: taskRequests(), inFlight: C.drawerDetailInFlightSeq };

  // 2. The drawer opens, and its body lands.
  requests.length = 0;
  C.openDrawer(PROJECT, PROJECT.tasks[0]);
  return settle();
}).then(function () {
  out.onOpen = { taskRequests: taskRequests(), ticks: ticks("acceptanceCriteria"),
                 sections: sections(), inFlight: C.drawerDetailInFlightSeq };

  // 3. A tick whose payload is byte-identical: it asks, and then leaves
  //    the body exactly as the viewer left it -- their fold, their
  //    scroll offset, their keyboard focus.
  toggleSection("description");
  body().scrollTop = 137;
  var box = firstBox("acceptanceCriteria");
  box.focus();
  stamp();
  requests.length = 0;
  C.doRefresh(false);
  return settle().then(function () {
    out.unchangedTick = {
      taskRequests: taskRequests(),
      stamps: stampsIntact(),
      scrollTop: body().scrollTop,
      focusHeld: box.focused && reachable(box),
      sections: sections()
    };
  });
}).then(function () {
  // 4. An agent ticks a criterion on disk. No reopen, no reload.
  setView("TASK-126", { checked: true, branchStatus: "Done", notes: "Done on the branch." });
  stamp();
  requests.length = 0;
  C.doRefresh(false);
  return settle();
}).then(function () {
  out.tickedOnDisk = {
    taskRequests: taskRequests(),
    ticks: ticks("acceptanceCriteria"),
    header: sectionNode("acceptanceCriteria").childNodes[0].textContent,
    stamps: stampsIntact(),
    sections: sections(),
    branchStatus: C.currentDrawer.branchTask && C.currentDrawer.branchTask.status,
    branchTicks: ticks("branch"),
    detailNotes: C.currentDrawer.detail && C.currentDrawer.detail.implementationNotes,
    notesOnScreen: bodyText().indexOf("Done on the branch.") !== -1
  };

  // 5. A refresh held mid-flight while the viewer moves to another
  //    task: the response lands too late and is thrown away.
  HOLD = true;
  setView("TASK-126", { notes: "NEVER RENDERED" });
  requests.length = 0;
  C.doRefresh(false);
  return settle();
}).then(function () {
  HOLD = false;
  out.supersededSent = taskRequests().length;
  C.openDrawer(PROJECT, PROJECT.tasks[1]);   // ...and the drawer moves on
  return settle();
}).then(function () {
  release();                                  // the stale response finally lands
  return settle();
}).then(function () {
  out.superseded = {
    drawer: C.currentDrawer.id,
    detail: C.currentDrawer.detail.id,
    staleNotesOnScreen: bodyText().indexOf("NEVER RENDERED") !== -1,
    inFlight: C.drawerDetailInFlightSeq
  };

  // 6. The drawer closes: the refresh stops with it.
  C.byId("drawer-close").click();
  requests.length = 0;
  C.doRefresh(false);
  return settle();
}).then(function () {
  out.afterClose = { taskRequests: taskRequests(), drawer: C.currentDrawer };

  // 7. A refresh that fails leaves the last good body alone -- the
  //    viewer is mid-read.
  setView("TASK-126");
  C.openDrawer(PROJECT, PROJECT.tasks[0]);
  return settle();
}).then(function () {
  stamp();
  TASK_FETCH_FAILS = true;
  requests.length = 0;
  C.doRefresh(false);
  return settle();
}).then(function () {
  out.failedRefresh = {
    taskRequests: taskRequests(),
    stamps: stampsIntact(),
    sections: sections(),
    errorShown: !!C.byId("drawer-detail-error"),
    inFlight: C.drawerDetailInFlightSeq
  };

  // 8. The open-time error path is untouched: a body that never loaded
  //    still says so...
  C.byId("drawer-close").click();
  C.openDrawer(PROJECT, PROJECT.tasks[0]);
  return settle();
}).then(function () {
  var err = C.byId("drawer-detail-error");
  out.failedOpen = { error: err ? err.textContent : null, sections: sections() };

  // ...and the next tick that succeeds is what gets the viewer out of it.
  setView("TASK-126", { checked: true });
  TASK_FETCH_FAILS = false;
  C.doRefresh(false);
  return settle();
}).then(function () {
  out.recoveredAfterFailedOpen = {
    errorShown: !!C.byId("drawer-detail-error"),
    sections: sections(),
    ticks: ticks("acceptanceCriteria")
  };
  // Eight refresh ticks and four drawer opens later: what got armed.
  out.timers = timers;
  require("fs").writeSync(1, JSON.stringify(out));
  process.exit(0);
}).catch(function (err) {
  process.stderr.write((err && err.stack) || String(err));
  process.exit(1);
});
"""


@js_harness.requires_node
class DrawerBodyRefreshBehaviourTests(unittest.TestCase):
    """Task-126: the drawer's body follows the task file while it is open.

    Every claim here is about what a REFRESH TICK does to a drawer that
    is already on screen, so all of it runs against one drawer walked
    through eight ticks: the fetch that is and is not issued, the DOM
    that is and is not rebuilt, and the two responses (a superseded one,
    a failed one) that must change nothing at all."""

    @property
    def out(self):
        return js_harness.cached_driver(self, DRAWER_BODY_REFRESH_DRIVER_JS)

    # -- AC #5: only while a drawer is open --

    def test_the_refresh_arms_no_timer_of_its_own(self):
        # It rides the countdown api.js already arms at load. A body
        # refresh that scheduled its own next tick would be a second
        # cadence beside the pane's one poller, and would appear here.
        self.assertEqual(self.out["timers"],
                         [{"kind": "interval", "ms": 1000}])

    def test_a_tick_with_no_drawer_open_asks_for_no_task_body(self):
        self.assertEqual(self.out["closedTick"]["taskRequests"], [])
        self.assertEqual(self.out["closedTick"]["inFlight"], 0)

    def test_closing_the_drawer_stops_the_refresh(self):
        # Not "unschedules a timer" -- there is none. With no drawer the
        # call returns before it fetches, which is the same thing said
        # once instead of in two places that could disagree.
        self.assertIsNone(self.out["afterClose"]["drawer"])
        self.assertEqual(self.out["afterClose"]["taskRequests"], [])

    # -- AC #1: the change appears without a reopen --

    def test_the_open_fetches_the_body_once(self):
        self.assertEqual(self.out["onOpen"]["taskRequests"],
                         ["GET /api/task?project=my-tool&id=TASK-126"])
        self.assertEqual(self.out["onOpen"]["ticks"], [False, False])
        self.assertEqual(self.out["onOpen"]["inFlight"], 0)

    def test_a_criterion_ticked_on_disk_ticks_itself_in_the_open_drawer(self):
        ticked = self.out["tickedOnDisk"]
        # One tick of the ordinary refresh, no reopen anywhere in the
        # flow above -- the checkbox and its count both move.
        self.assertEqual(ticked["taskRequests"],
                         ["GET /api/task?project=my-tool&id=TASK-126"])
        self.assertEqual(ticked["ticks"], [True, False])
        self.assertIn("1/2", ticked["header"])
        self.assertTrue(ticked["notesOnScreen"])

    # -- AC #2: the branch-side copy rides the same pass --

    def test_the_branch_copy_behind_the_rail_and_the_actions_follows_too(self):
        ticked = self.out["tickedOnDisk"]
        # currentDrawer.branchTask is what the Resume-vs-Re-spawn
        # decision (task-116) and the "likely finished" fallback
        # (task-38) read; currentDrawer.detail is what the theater's
        # task rail (task-93) renders from. Both are stashed by the
        # same response.
        self.assertEqual(ticked["branchStatus"], "Done")
        self.assertEqual(ticked["detailNotes"], "Done on the branch.")
        self.assertEqual(ticked["branchTicks"], [True, False])

    # -- AC #3: an unchanged payload touches nothing --

    def test_an_unchanged_tick_asks_and_then_changes_no_node_at_all(self):
        tick = self.out["unchangedTick"]
        # It really did ask -- this is not a test of a fetch that was
        # skipped...
        self.assertEqual(tick["taskRequests"],
                         ["GET /api/task?project=my-tool&id=TASK-126"])
        # ...and then every node the body held before the tick is still
        # the same object afterwards.
        self.assertTrue(tick["stamps"]["intact"])
        self.assertGreater(tick["stamps"]["nodes"], 10)

    def test_an_unchanged_tick_keeps_the_scroll_offset_and_the_focus(self):
        tick = self.out["unchangedTick"]
        self.assertEqual(tick["scrollTop"], 137)
        self.assertTrue(tick["focusHeld"])

    def test_a_section_the_viewer_folded_stays_folded_across_both_kinds_of_tick(self):
        # Unchanged: nothing was rebuilt, so nothing could re-fold it.
        folded = [s for s in self.out["unchangedTick"]["sections"] if s["collapsed"]]
        self.assertEqual([s["key"] for s in folded], ["description"])
        # Changed: the body IS rebuilt, and the viewer's own toggle
        # (stored per section key) still outranks the auto-fold default.
        folded = [s for s in self.out["tickedOnDisk"]["sections"] if s["collapsed"]]
        self.assertEqual([s["key"] for s in folded], ["description"])

    def test_a_changed_payload_does_rebuild_the_body(self):
        # The other half of the stamp probe: it can tell the two apart.
        self.assertFalse(self.out["tickedOnDisk"]["stamps"]["intact"])

    # -- AC #4: a superseded response is discarded --

    def test_a_response_that_lands_after_the_drawer_moved_on_is_thrown_away(self):
        self.assertEqual(self.out["supersededSent"], 1)   # it was sent...
        sup = self.out["superseded"]
        self.assertEqual(sup["drawer"], "TASK-99")
        self.assertEqual(sup["detail"], "TASK-99")        # ...and never stashed
        self.assertFalse(sup["staleNotesOnScreen"])       # ...and never rendered
        self.assertEqual(sup["inFlight"], 0)              # ...and left nothing wedged

    # -- AC #6: a failed refresh is silent --

    def test_a_failed_refresh_leaves_the_last_good_body_on_screen(self):
        failed = self.out["failedRefresh"]
        self.assertEqual(len(failed["taskRequests"]), 1)
        self.assertTrue(failed["stamps"]["intact"])
        self.assertFalse(failed["errorShown"])
        self.assertEqual([s["key"] for s in failed["sections"]],
                         ["description", "acceptanceCriteria", "dependencies"])
        self.assertEqual(failed["inFlight"], 0)

    def test_the_open_time_error_path_is_unchanged(self):
        # A body that never loaded still says why, and builds no sections.
        self.assertIn("Failed to load task details: network down",
                      self.out["failedOpen"]["error"])
        self.assertEqual(self.out["failedOpen"]["sections"], [])
        # And the next tick that succeeds is what gets the viewer out of
        # it -- a failed open is no longer a dead drawer until reopened.
        recovered = self.out["recoveredAfterFailedOpen"]
        self.assertFalse(recovered["errorShown"])
        self.assertEqual(recovered["ticks"], [True, False])


# ---------------------------------------------------------------------
# task-127: the viewer's place, carried across a rebuild
# ---------------------------------------------------------------------

# task-126 protected the QUIET tick: an identical payload returns before
# touching any DOM. This drives the other half -- the tick that carries
# a change, and therefore rebuilds the body under a viewer who is
# somewhere in the middle of it. The claims are about two things the
# rebuild used to destroy, scroll offset and keyboard focus, so the
# driver plays a viewer: it scrolls, it focuses, it lets a change land,
# and it reads back where it ended up.
#
# Height is real here rather than assumed. The shim's scrollHeight is a
# plain number, which would make a clamp untestable (nothing shrinks),
# so this driver alone defines #drawer-body's scrollHeight as a function
# of how many nodes the body actually holds: a payload that renders less
# genuinely produces a shorter scroll range, and the clamp either
# happens or does not.
DRAWER_BODY_VIEW_DRIVER_JS = js_harness.LOAD_SOURCES_JS + r"""
var timers = [];
global.setInterval = function (fn, ms) { timers.push({ kind: "interval", ms: ms }); return {}; };
global.setTimeout = function (fn, ms) { timers.push({ kind: "timeout", ms: ms }); return {}; };
global.clearTimeout = function () {};

var C = loadFrontend(["state.js", "dom.js", "api.js", "tasks.js", "board.js",
                      "spawn.js", "harvest.js", "drawer.js", "pane.js"]);

C.renderProjectChips = function () {};
C.renderErrorBanners = function () {};
C.renderMilestoneFilter = function () {};
C.renderMilestoneChipLabel = function () {};
C.renderSessionsPanel = function () {};
C.renderBoard = function () {};
C.renderBoardAndSessionsIfChanged = function () {};
C.renderVersion = function () {};
C.showToast = function () {};
C.copyText = function () {};
wireDrawerShell();

var STATUSES = ["To Do", "In Progress", "Done"];
function boardTask(id) {
  return { id: id, title: "A task", status: "In Progress", ready: true,
           hasSpawnBranch: false, alreadyMerged: false, worktreeDirty: false };
}
var PROJECT = { name: "my-tool", statuses: STATUSES,
                tasks: [boardTask("TASK-127"), boardTask("TASK-99")] };
var BOARD = { projects: [PROJECT], capabilities: { tmux: true } };
C.knownProjects.length = 0;
C.knownProjects.push("my-tool");
C.activeProjects = new Set(["my-tool"]);
C.boardData = BOARD;
C.sessionsData = [];
C.firstLoadDone = true;

// What GET /api/task answers for a task right now. `opts` is the disk
// changing between ticks: a criterion ticked, dependencies removed, the
// whole ticket cut down to nothing.
var VIEWS = {};
function setView(id, opts) {
  opts = opts || {};
  VIEWS[id] = {
    task: { id: id, title: "A task", status: "In Progress",
            description: opts.short ? "" : "A description of a few words.",
            acceptanceCriteria: opts.short ? [] : [
              { text: "the offset survives", checked: !!opts.checked },
              { text: "the focus survives", checked: false }
            ],
            dependencies: (opts.short || opts.noDeps) ? [] : ["TASK-99", "TASK-88"],
            implementationNotes: opts.short ? "" : "Some notes worth reading." },
    branchTask: null
  };
}
setView("TASK-127");
setView("TASK-99");

var requests = [];
global.fetch = function (url, opts) {
  requests.push(((opts && opts.method) || "GET") + " " + url);
  url = String(url);
  function answer(data) {
    var copy = JSON.parse(JSON.stringify(data));
    return { ok: true, status: 200, json: function () { return Promise.resolve(copy); } };
  }
  if (url.indexOf("/api/task") === 0) {
    return Promise.resolve(answer(VIEWS[decodeURIComponent(url.split("&id=")[1])]));
  }
  if (url.indexOf("/api/board") === 0) return Promise.resolve(answer(BOARD));
  return Promise.resolve(answer({ sessions: [] }));
};

// -- a body with a height that follows its content --
function body() { return C.byId("drawer-body"); }
function nodeCount(n) {
  var count = 0;
  (function walk(x) { count++; x.childNodes.forEach(walk); })(n);
  return count;
}
body().clientHeight = 100;
Object.defineProperty(body(), "scrollHeight", {
  get: function () { return nodeCount(body()) * 20; }
});
function maxScroll() { return Math.max(0, body().scrollHeight - body().clientHeight); }

function sectionNode(key) {
  return body().childNodes.filter(function (n) {
    return n.attrs && n.attrs["data-section"] === key;
  })[0] || null;
}
function sections() {
  return body().childNodes
    .filter(function (n) { return n.attrs && n.attrs["data-section"]; })
    .map(function (s) {
      return { key: s.attrs["data-section"],
               collapsed: String(s.className).split(/\s+/).indexOf("collapsed") !== -1 };
    });
}
function toggleOf(key) {
  var node = sectionNode(key);
  return node ? node.childNodes[0].childNodes[0] : null;
}
function depRow(id) {
  return body().querySelectorAll("[data-dep]").filter(function (r) {
    return r.getAttribute("data-dep") === id;
  })[0] || null;
}
function ticks(key) {
  var out = [];
  var node = sectionNode(key);
  if (node) (function walk(n) {
    if (String(n.tagName).toLowerCase() === "input") out.push(!!n.checked);
    n.childNodes.forEach(walk);
  })(node);
  return out;
}
function reachable(target) {
  var hit = false;
  if (!target) return false;
  (function walk(n) { if (n === target) hit = true; n.childNodes.forEach(walk); })(body());
  return hit;
}
// Where focus actually ended up, named the way a reader would name it
// rather than as an object identity: which section's toggle, which
// dependency row, the body itself, or something outside the body.
function focusReport() {
  var el = document.activeElement;
  var parent = el && el.parentNode;
  var grand = parent && parent.parentNode;
  return {
    isBody: el === body(),
    inBody: reachable(el),
    tag: el ? String(el.tagName) : null,
    dep: el && el.getAttribute ? el.getAttribute("data-dep") : null,
    toggleOf: (el && String(el.className || "").indexOf("drawer-section-toggle") !== -1
               && grand && grand.getAttribute) ? grand.getAttribute("data-section") : null,
    id: el ? (el.id || null) : null
  };
}

var stampSeq = 0;
function stamp() {
  stampSeq++;
  (function walk(n) { n.__stamp = stampSeq; n.childNodes.forEach(walk); })(body());
}
function stampsIntact() {
  var intact = true;
  (function walk(n) {
    if (n.__stamp !== stampSeq) intact = false;
    n.childNodes.forEach(walk);
  })(body());
  return intact;
}

var out = {};

// 1. The drawer opens on a body the viewer had left scrolled: an open
//    starts at the top, whatever was there before.
body().scrollTop = 400;
C.openDrawer(PROJECT, PROJECT.tasks[0]);
settle().then(function () {
  out.onOpen = { scrollTop: body().scrollTop, focus: focusReport(),
                 sections: sections().map(function (s) { return s.key; }),
                 maxScroll: maxScroll() };

  // 2. The viewer reads down the ticket, folds a section by hand, and
  //    puts focus on the Acceptance Criteria toggle. Then an agent ticks
  //    a criterion on disk and the ordinary tick brings it in.
  toggleOf("description").click();
  body().scrollTop = 120;
  toggleOf("acceptanceCriteria").focus();
  stamp();
  setView("TASK-127", { checked: true });
  C.doRefresh(false);
  return settle();
}).then(function () {
  out.changedTick = {
    rebuilt: !stampsIntact(),          // the body really was rebuilt...
    ticks: ticks("acceptanceCriteria"), // ...with the change in it...
    scrollTop: body().scrollTop,        // ...and the viewer did not move
    focus: focusReport(),
    focusIsLiveNode: reachable(document.activeElement),
    sections: sections()
  };

  // 3. The ticket is cut down to almost nothing while the viewer is at
  //    the very bottom of it: the offset they had no longer exists.
  body().scrollTop = maxScroll();
  out.beforeShrink = { scrollTop: body().scrollTop, maxScroll: maxScroll() };
  setView("TASK-127", { short: true });
  C.doRefresh(false);
  return settle();
}).then(function () {
  out.shrunk = { scrollTop: body().scrollTop, maxScroll: maxScroll() };

  // 4. Back to the full ticket, and focus on a dependency row: the row
  //    is still there after a change, so focus is still on it.
  setView("TASK-127");
  C.doRefresh(false);
  return settle();
}).then(function () {
  body().scrollTop = 60;
  depRow("TASK-99").focus();
  setView("TASK-127", { checked: true });
  C.doRefresh(false);
  return settle();
}).then(function () {
  out.depKept = { focus: focusReport(), scrollTop: body().scrollTop,
                  focusIsLiveNode: reachable(document.activeElement) };

  // 5. ...and now the dependency is removed while focus is on it. The
  //    element it was on is gone, so focus cannot be put back on it --
  //    what must not happen is focus escaping the drawer.
  depRow("TASK-99").focus();
  setView("TASK-127", { noDeps: true });
  C.doRefresh(false);
  return settle();
}).then(function () {
  out.depGone = { focus: focusReport(), deps: !!depRow("TASK-99") };

  // 6. Focus outside the body entirely (the drawer's close button) is
  //    not the drawer body's to move.
  setView("TASK-127", { checked: true });
  C.byId("drawer-close").focus();
  C.doRefresh(false);
  return settle();
}).then(function () {
  out.focusOutside = { focus: focusReport() };

  // 7. The quiet path task-126 built, unchanged: an identical payload
  //    still returns before touching any DOM, so none of the above runs.
  body().scrollTop = 77;
  toggleOf("acceptanceCriteria").focus();
  var held = document.activeElement;
  stamp();
  requests.length = 0;
  C.doRefresh(false);
  return settle().then(function () {
    out.quietTick = {
      asked: requests.filter(function (r) { return r.indexOf("GET /api/task") === 0; }).length,
      stampsIntact: stampsIntact(),
      scrollTop: body().scrollTop,
      sameFocusNode: document.activeElement === held,
      focus: focusReport()
    };
  });
}).then(function () {
  // 8. The viewer clicks through to another task: a different ticket
  //    starts at the top, with no focus carried into it.
  body().scrollTop = 150;
  toggleOf("acceptanceCriteria").focus();
  C.openDrawer(PROJECT, PROJECT.tasks[1]);
  return settle();
}).then(function () {
  out.switched = { id: C.currentDrawer.id, scrollTop: body().scrollTop,
                   focusInBody: reachable(document.activeElement) };
  out.timers = timers;
  require("fs").writeSync(1, JSON.stringify(out));
  process.exit(0);
}).catch(function (err) {
  process.stderr.write((err && err.stack) || String(err));
  process.exit(1);
});
"""


@js_harness.requires_node
class DrawerBodyViewPreservationBehaviourTests(unittest.TestCase):
    """Task-127: a refresh that carries a change keeps the viewer's place.

    One drawer, walked through the cases that differ only in what the
    change did to the element under the viewer: it survived, it shrank
    away beneath them, it was deleted out from under their focus, or it
    was never theirs to move in the first place."""

    @property
    def out(self):
        return js_harness.cached_driver(self, DRAWER_BODY_VIEW_DRIVER_JS)

    # -- AC #1: a changed payload leaves the body where the viewer left it --

    def test_a_changed_payload_rebuilds_the_body_and_keeps_the_offset(self):
        tick = self.out["changedTick"]
        # The rebuild is real -- not a test of a render that was skipped.
        self.assertTrue(tick["rebuilt"])
        self.assertEqual(tick["ticks"], [True, False])
        # ...and the viewer is still looking at what they were reading.
        self.assertEqual(tick["scrollTop"], 120)

    # -- AC #3: focus follows the element it was on --

    def test_focus_on_a_section_toggle_survives_the_rebuild(self):
        tick = self.out["changedTick"]
        self.assertEqual(tick["focus"]["toggleOf"], "acceptanceCriteria")
        self.assertEqual(tick["focus"]["tag"], "button")
        # The node it landed on is one of the NEW body's, not the
        # detached one the rebuild threw away.
        self.assertTrue(tick["focusIsLiveNode"])

    def test_focus_on_a_dependency_row_survives_by_its_id(self):
        kept = self.out["depKept"]
        self.assertEqual(kept["focus"]["dep"], "TASK-99")
        self.assertTrue(kept["focusIsLiveNode"])
        self.assertEqual(kept["scrollTop"], 60)

    def test_focus_whose_element_is_gone_stays_inside_the_drawer(self):
        gone = self.out["depGone"]
        self.assertFalse(gone["deps"])          # the row really did go...
        self.assertTrue(gone["focus"]["isBody"])
        # ...and focus is on the drawer's own body, not loose on the
        # document behind it, where the next Tab would walk the board.
        self.assertEqual(gone["focus"]["id"], "drawer-body")

    def test_focus_outside_the_body_is_left_alone(self):
        self.assertEqual(self.out["focusOutside"]["focus"]["id"], "drawer-close")
        self.assertFalse(self.out["focusOutside"]["focus"]["inBody"])

    # -- AC #2: the offset is clamped to the new content height --

    def test_a_body_that_shrank_settles_at_its_new_bottom(self):
        before, after = self.out["beforeShrink"], self.out["shrunk"]
        # The ticket really did get shorter, and the offset the viewer
        # had is past the end of what is left.
        self.assertLess(after["maxScroll"], before["scrollTop"])
        # So it settles at the new bottom rather than holding an offset
        # that no longer exists.
        self.assertEqual(after["scrollTop"], after["maxScroll"])

    # -- AC #4: an open, and a switch, start at the top --

    def test_opening_a_drawer_starts_at_the_top_with_no_focus_restored(self):
        opened = self.out["onOpen"]
        self.assertEqual(opened["scrollTop"], 0)   # despite the 400 before it
        self.assertGreater(opened["maxScroll"], 400)
        self.assertFalse(opened["focus"]["inBody"])

    def test_switching_to_another_task_starts_at_the_top_too(self):
        switched = self.out["switched"]
        self.assertEqual(switched["id"], "TASK-99")
        self.assertEqual(switched["scrollTop"], 0)
        self.assertFalse(switched["focusInBody"])

    # -- AC #5 and #6: the quiet path and the folds are unchanged --

    def test_an_identical_payload_still_touches_no_dom_at_all(self):
        quiet = self.out["quietTick"]
        self.assertEqual(quiet["asked"], 1)        # it did ask...
        self.assertTrue(quiet["stampsIntact"])     # ...and changed nothing
        self.assertEqual(quiet["scrollTop"], 77)
        self.assertTrue(quiet["sameFocusNode"])

    def test_a_folded_section_stays_folded_across_the_changed_tick(self):
        folded = [s["key"] for s in self.out["changedTick"]["sections"] if s["collapsed"]]
        self.assertEqual(folded, ["description"])

    def test_the_preservation_arms_no_timer_of_its_own(self):
        self.assertEqual(self.out["timers"], [{"kind": "interval", "ms": 1000}])


# ---------------------------------------------------------------------
# task-134: what the armed spawn confirm SAYS after a discard
# ---------------------------------------------------------------------

# The guard that fires here (isOutOfBoardClaim) sees the same three
# fields in both cases -- In Progress, no session, no branch -- so the
# only thing that can tell "someone else is on this" apart from "you
# threw the last attempt away nine seconds ago" is the recovery tag the
# discard left behind, reaching the frontend as lastDiscardedAt. This
# drives the REAL card button through both cases: one click to read the
# armed copy, a second to prove the confirm still gates a real spawn.
#
# TZ and Date.now are both pinned, because the copy is deliberately a
# local wall clock ("17:05 today") rather than an elapsed count.
SPAWN_CLAIM_COPY_DRIVER_JS = 'process.env.TZ = "UTC";\n' + js_harness.LOAD_SOURCES_JS + r"""
var C = loadFrontend(["state.js", "dom.js", "tasks.js", "board.js",
                      "spawn.js", "harvest.js", "drawer.js"]);

C.renderSessionsPanel = function () {};
C.syncDrawerPanePolling = function () {};
C.renderProjectChips = function () {};
C.renderErrorBanners = function () {};
C.doRefresh = function () {};
C.fetchSessions = function () {};
C.showToast = function () {};

// 2026-09-05T09:14:00Z. The discards below sit at 17:05 the previous
// evening, 09:05 this morning, and 17:05 four days back.
var NOW = Date.UTC(2026, 8, 5, 9, 14, 0);
Date.now = function () { return NOW; };

var requests = [];
global.fetch = function (url, opts) {
  requests.push(((opts && opts.method) || "GET") + " " + url);
  return Promise.resolve({
    ok: true, status: 200,
    json: function () { return Promise.resolve({ session: "s", attach: "a", agent: "claude" }); }
  });
};

// One card per case, all four in the guard's shape (In Progress, ready,
// no live session) except `branch`, which has a branch and so is not.
var SCENARIOS = [
  { name: "claimed",    discardedAt: null },
  { name: "today",      discardedAt: "2026-09-05T09:05:00Z" },
  { name: "yesterday",  discardedAt: "2026-09-04T17:05:31Z" },
  { name: "older",      discardedAt: "2026-09-01T17:05:31Z" },
  { name: "unparsable", discardedAt: "not a timestamp" },
  { name: "branch",     discardedAt: "2026-09-05T09:05:00Z", hasSpawnBranch: true }
];
var TASKS = SCENARIOS.map(function (s, i) {
  return {
    id: "TASK-" + (i + 1), title: "A task", status: "In Progress", ready: true,
    assignees: ["@claude-opus"], updatedAt: "2026-09-05T08:50:00Z",
    hasSpawnBranch: !!s.hasSpawnBranch, alreadyMerged: false, worktreeDirty: false,
    branchCheckout: s.hasSpawnBranch ? { kind: "centrale", path: "/wt" } : null,
    lastDiscardedAt: s.discardedAt
  };
});
var PROJECT = { name: "my-tool", tasks: TASKS, statuses: ["To Do", "In Progress", "Done"] };

function reset() {
  C.knownProjects.length = 0;
  C.knownProjects.push("my-tool");
  C.activeProjects = new Set(["my-tool"]);
  C.boardData = { projects: [PROJECT], capabilities: { tmux: true } };
  C.sessionsData = [];
  C.firstLoadDone = true;
  [C.harvestStates, C.spawnStates, C.cleanupStates, C.spawnConfirmPending].forEach(function (m) {
    Object.keys(m).forEach(function (k) { delete m[k]; });
  });
  requests.length = 0;
  C.renderBoard();
}

function cardOf(name) {
  var id = "TASK-" + (SCENARIOS.map(function (s) { return s.name; }).indexOf(name) + 1);
  var found = null;
  (function walk(n) {
    if (String(n.className || "").split(/\s+/).indexOf("card") !== -1) {
      var idNode = n.childNodes.filter(function (c) {
        return String(c.className || "").split(/\s+/).indexOf("card-id") !== -1;
      })[0];
      if (idNode && idNode.textContent === id) found = n;
      return;
    }
    n.childNodes.forEach(walk);
  })(C.byId("board"));
  return found;
}

function spawnButton(name) {
  var card = cardOf(name);
  if (!card) return null;
  return card.querySelectorAll("button").filter(function (b) {
    return String(b.className || "").indexOf("spawn-inline") !== -1;
  })[0] || null;
}

function read(b) {
  return b ? { text: b.textContent, title: b.title || "",
               confirming: String(b.className).indexOf("confirming") !== -1 } : null;
}

var out = { unarmed: {}, armed: {}, secondClick: {}, drawerLabel: {}, allButtons: {} };
SCENARIOS.forEach(function (s) {
  reset();
  out.allButtons[s.name] = cardOf(s.name).querySelectorAll("button").map(function (b) {
    return b.textContent;
  });
  out.unarmed[s.name] = read(spawnButton(s.name));
  // A task with a branch is never offered Spawn at all (renderCard), so
  // there is nothing here to arm -- the card's one footer button is
  // Merge, and the guard is never consulted.
  if (s.hasSpawnBranch) { out.armed[s.name] = null; out.secondClick[s.name] = null; return; }
  var btn = spawnButton(s.name);
  btn.click();
  out.armed[s.name] = read(spawnButton(s.name));
  out.firstClickRequests = out.firstClickRequests || {};
  out.firstClickRequests[s.name] = requests.slice();
  // The drawer, off the same armed pending entry: its wider button
  // renders the LONG form as its own label rather than as a tooltip.
  var id = "TASK-" + (SCENARIOS.indexOf(s) + 1);
  C.currentDrawer = { project: "my-tool", id: id, summary: C.findTask("my-tool", id) };
  C.renderDrawerSpawnArea();
  out.drawerLabel[s.name] = read(C.byId("drawer-spawn-area").querySelectorAll("button")[0]);
  C.currentDrawer = null;
  requests.length = 0;
  spawnButton(s.name).click();
  out.secondClick[s.name] = requests.slice();
});

process.stdout.write(JSON.stringify(out));
"""


@js_harness.requires_node
class SpawnClaimCopyTests(unittest.TestCase):
    """task-134: the armed confirm names the discard when there was one.

    Read off the real card button the real render path built, in both
    branches of the rule and with the second click still doing the
    spawning.
    """

    @property
    def out(self):
        return js_harness.cached_driver(self, SPAWN_CLAIM_COPY_DRIVER_JS, timeout=30)

    # -- AC #1: the discard is named, with its time --

    def test_a_discard_today_is_named_by_its_clock_time_not_as_a_claim(self):
        armed = self.out["armed"]["today"]
        self.assertEqual(
            armed["title"],
            "Previous attempt discarded 09:05 today; still In Progress on main."
            " Spawn fresh?")
        self.assertEqual(armed["text"], "Discarded — spawn fresh?")
        self.assertNotIn("claimed by", armed["title"])

    def test_yesterdays_discard_says_yesterday(self):
        self.assertEqual(
            self.out["armed"]["yesterday"]["title"],
            "Previous attempt discarded 17:05 yesterday; still In Progress on main."
            " Spawn fresh?")

    def test_an_older_discard_carries_its_date(self):
        self.assertEqual(
            self.out["armed"]["older"]["title"],
            "Previous attempt discarded 17:05 on 1 Sep; still In Progress on main."
            " Spawn fresh?")

    def test_the_drawers_own_button_carries_the_same_sentence_as_its_label(self):
        drawer = self.out["drawerLabel"]["today"]
        self.assertEqual(drawer["text"], self.out["armed"]["today"]["title"])
        self.assertTrue(drawer["confirming"])
        # And the claimed-elsewhere case is unchanged there too.
        self.assertEqual(self.out["drawerLabel"]["claimed"]["text"],
                         self.out["armed"]["claimed"]["title"])

    # -- AC #2: the guard still gates, and the second click still spawns --

    def test_the_confirm_still_arms_and_the_first_click_spawns_nothing(self):
        self.assertTrue(self.out["armed"]["today"]["confirming"])
        self.assertEqual(self.out["firstClickRequests"]["today"], [])

    def test_the_second_click_inside_the_window_spawns(self):
        self.assertEqual(self.out["secondClick"]["today"], ["POST /api/spawn"])
        # ...exactly as it does for a real out-of-board claim.
        self.assertEqual(self.out["secondClick"]["claimed"], ["POST /api/spawn"])

    # -- AC #3: no tag, or a branch, and nothing changed --

    def test_without_a_recovery_tag_the_copy_is_the_claimed_elsewhere_one(self):
        armed = self.out["armed"]["claimed"]
        self.assertEqual(armed["text"], "Claimed elsewhere — spawn anyway?")
        self.assertEqual(
            armed["title"],
            "In Progress — claimed by @claude-opus, updated 24 minutes ago."
            " Spawn anyway?")

    def test_an_unparsable_timestamp_falls_back_to_that_same_copy(self):
        self.assertEqual(self.out["armed"]["unparsable"],
                         self.out["armed"]["claimed"])

    def test_a_task_with_a_branch_is_not_offered_spawn_at_all(self):
        # The guard never reaches the copy for a branch-bearing task,
        # even though its lastDiscardedAt is set: the card offers Merge
        # and nothing else (see renderCard), so there is nothing to arm.
        self.assertEqual(self.out["allButtons"]["branch"], ["Merge"])
        self.assertIsNone(self.out["armed"]["branch"])
        # Every other case offers exactly the one Spawn button.
        for name in ("claimed", "today", "yesterday", "older", "unparsable"):
            self.assertEqual(self.out["allButtons"][name], ["Spawn (claude-opus)"], name)

    def test_the_unarmed_button_is_the_ordinary_spawn_label_in_every_case(self):
        for name in ("claimed", "today", "yesterday", "older", "unparsable"):
            self.assertEqual(self.out["unarmed"][name]["text"], "Spawn (claude-opus)", name)
            self.assertFalse(self.out["unarmed"][name]["confirming"], name)


# ---------------------------------------------------------------------
# task-157: the board you just opened may be running an older backlog
# ---------------------------------------------------------------------

BOARD_VERSION_DRIFT_DRIVER_JS = js_harness.LOAD_SOURCES_JS + r"""
var C = loadFrontend(["state.js", "dom.js", "feedback.js"]);

// Timers are the driver's: a sticky toast is one that armed NO
// auto-dismiss, which is only observable if nothing here waits.
var timers = [];
global.setTimeout = function (fn, ms) { timers.push({ fn: fn, ms: ms }); return timers.length; };

// window.open is the shim's one gap here -- feedback.js opens the board
// in a new tab, which is also why the notice about it has to survive
// longer than a glance at this one.
var opened = [];
global.window.open = function (url) { opened.push(url); };

var requests = [];
var payload = { url: "http://127.0.0.1:6421", versionDrift: null };
var responseOk = true;
global.fetch = function (url, opts) {
  requests.push(((opts && opts.method) || "GET") + " " + url);
  return Promise.resolve({
    ok: responseOk, status: responseOk ? 200 : 500,
    json: function () {
      return Promise.resolve(responseOk ? payload : { error: "failed to launch" });
    }
  });
};

function toasts() {
  return document.getElementById("toast-container").childNodes.map(function (t) {
    var msg = t.childNodes[0];
    return { className: t.className, text: msg ? msg.textContent : "" };
  });
}

function clearToasts() {
  var box = document.getElementById("toast-container");
  while (box.childNodes.length) box.removeChild(box.childNodes[0]);
}

function open(next, taskId) {
  clearToasts();
  timers = [];
  opened = [];
  requests = [];
  if (next !== undefined) payload = next;
  C.openProjectBoard("my-tool", null, taskId);
  return settle();
}

var out = {};

open({ url: "http://127.0.0.1:6421", versionDrift: null }).then(function () {
  // The normal case: the board opens and nothing is said about it.
  out.agreeing = { toasts: toasts(), opened: opened, requests: requests, timers: timers.length };
  return open({
    url: "http://127.0.0.1:6421",
    versionDrift: { running: "1.50.1", cli: "1.51.0" }
  });
}).then(function () {
  out.drift = { toasts: toasts(), opened: opened, requests: requests,
                timerDelays: timers.map(function (t) { return t.ms; }) };
  // The ✕ is the only way out of a sticky toast, and it still works.
  var box = document.getElementById("toast-container");
  box.childNodes[0].childNodes[1].click();
  out.afterDismiss = toasts();
  // A drift alongside a task id: the deep link is unaffected.
  return open({
    url: "http://127.0.0.1:6421",
    versionDrift: { running: "1.50.1", cli: "1.51.0" }
  }, "TASK-157");
}).then(function () {
  out.driftWithTask = { toasts: toasts().length, opened: opened };
  // A half-filled field is not a signal.
  return open({ url: "http://127.0.0.1:6421", versionDrift: { running: "1.50.1" } });
}).then(function () {
  out.halfFilled = toasts();
  // An older server that reports no field at all.
  return open({ url: "http://127.0.0.1:6421" });
}).then(function () {
  out.fieldless = toasts();
  // A launch failure: the error toast is unchanged, and still expires.
  responseOk = false;
  return open();
}).then(function () {
  out.failure = { toasts: toasts(), opened: opened,
                  timerDelays: timers.map(function (t) { return t.ms; }) };
  responseOk = true;
  process.stdout.write(JSON.stringify(out));
}).catch(function (err) {
  process.stderr.write((err && err.stack) || String(err));
  process.exit(1);
});
"""


@js_harness.requires_node
class BoardVersionDriftBehaviourTests(unittest.TestCase):
    """Task-157: a `backlog browser` keeps the version it started with,
    so a board left running across a backlog.md upgrade quietly serves
    the old one. The frontend says so beside the board it just opened,
    names the fix, and does nothing else about it."""

    @property
    def out(self):
        return js_harness.cached_driver(self, BOARD_VERSION_DRIFT_DRIVER_JS)

    def test_a_board_on_the_same_version_is_opened_in_silence(self):
        agreeing = self.out["agreeing"]
        self.assertEqual(agreeing["toasts"], [])
        self.assertEqual(agreeing["opened"], ["http://127.0.0.1:6421"])
        self.assertEqual(agreeing["timers"], 0)

    def test_a_stale_board_is_named_with_both_versions_and_the_fix(self):
        toasts = self.out["drift"]["toasts"]
        self.assertEqual(len(toasts), 1)
        self.assertEqual(
            toasts[0]["text"],
            "my-tool's board is running backlog 1.50.1, but the backlog on PATH is 1.51.0 "
            "— a running board keeps the version it started with. Stop that board and "
            "open it again to pick up 1.51.0.")

    def test_the_notice_is_a_warning_rather_than_an_error(self):
        # A stale board is not a failed action: it opened, and it works.
        self.assertEqual(self.out["drift"]["toasts"][0]["className"], "toast warn")

    def test_the_notice_does_not_expire_while_the_user_is_in_the_other_tab(self):
        # Opening a board moves focus to the new tab, so the 6s
        # auto-dismiss every other toast arms would run out unseen. This
        # one arms no timer at all.
        self.assertEqual(self.out["drift"]["timerDelays"], [])
        # ...and the ✕ still closes it.
        self.assertEqual(self.out["afterDismiss"], [])

    def test_nothing_is_killed_restarted_or_reopened_on_a_difference(self):
        # The frontend's half of "report, never act": the board is opened
        # exactly once, the only request made is the one that opened it,
        # and there is no second call of any kind.
        drift = self.out["drift"]
        self.assertEqual(drift["opened"], ["http://127.0.0.1:6421"])
        self.assertEqual(drift["requests"], ["POST /api/browser"])

    def test_a_task_deep_link_still_lands_on_the_task(self):
        with_task = self.out["driftWithTask"]
        self.assertEqual(with_task["opened"], ["http://127.0.0.1:6421/board/TASK-157"])
        self.assertEqual(with_task["toasts"], 1)

    def test_a_half_filled_or_absent_field_says_nothing(self):
        self.assertEqual(self.out["halfFilled"], [])
        self.assertEqual(self.out["fieldless"], [])

    def test_a_launch_failure_still_raises_an_expiring_error_toast(self):
        # Sticky is opt-in: nothing else about toasts changed.
        failure = self.out["failure"]
        self.assertEqual(len(failure["toasts"]), 1)
        self.assertEqual(failure["toasts"][0]["className"], "toast error")
        self.assertIn("Failed to open board for my-tool", failure["toasts"][0]["text"])
        self.assertEqual(failure["opened"], [])
        self.assertEqual(failure["timerDelays"], [6000])



# ---------------------------------------------------------------------
# Removing a project, and the rows that used to outlive it (task-158)
# ---------------------------------------------------------------------

# The settings modal driven the way the bug was found: two projects,
# each with a check command, remove one, then Save -- without a reload
# in between. The defect was never visible in the source line by line;
# it was a relationship between two data sources inside one render (the
# fresh /api/settings payload, and the board's `C.knownProjects`, which
# is "every project name ever seen" and only ever grows). So the driver
# seeds `knownProjects` with a name the payload does not carry, exactly
# as a real session would after a removal, and watches which of the two
# the rows follow.
#
# The fake server mirrors settings.py: `current_settings` drops a
# removed project from BOTH `projects` and `checkCommands`, and a save
# naming a project projects.json does not have is refused with
# `fields["checkCommands.<name>"] = "unknown project"`.
SETTINGS_PROJECT_REMOVAL_DRIVER_JS = js_harness.LOAD_SOURCES_JS + r"""
// The driver owns timers: the confirm-arm disarm timer must not hold
// node's loop open for five seconds, and nothing here waits on one.
var armed = [];
global.setTimeout = function (fn, ms) { armed.push({ fn: fn, ms: ms }); return armed.length; };

var C = loadFrontend(["state.js", "dom.js", "tasks.js", "settings.js"]);
wireSettingsShell();

// The files this driver does not load, as sinks.
var toasts = [];
var refreshes = 0;
C.showToast = function (text, kind) { toasts.push({ text: text, kind: kind }); };
C.doRefresh = function () { refreshes++; };
C.syncDrawerPanePolling = function () {};

// -- the fake /api/settings --
var projects = [
  { name: "alpha", path: "/repos/alpha" },
  { name: "beta", path: "/repos/beta" }
];
var checkCommands = { alpha: "make test", beta: "pytest -q" };
var refuseNextSave = null;   // {field: reason}, to force a refusal
var requests = [];

function payload() {
  var commands = {};
  projects.forEach(function (p) { commands[p.name] = checkCommands[p.name] || null; });
  return {
    harvestMode: "click",
    sessionPreviewMode: "view",
    refreshIntervalSeconds: 30,
    checkCommands: commands,
    defaultAgent: "claude",
    agents: ["claude"],
    agentEntries: [{ name: "claude", cmdText: "claude", promptSuffix: "", builtin: true, onPath: true }],
    projects: projects.map(function (p) { return { name: p.name, path: p.path }; })
  };
}

function respond(status, data) {
  return Promise.resolve({
    ok: status < 400, status: status,
    json: function () { return Promise.resolve(data); }
  });
}

global.fetch = function (url, opts) {
  var body = opts && opts.body ? JSON.parse(opts.body) : null;
  requests.push({ url: url, method: (opts && opts.method) || "GET", body: body });
  if (!body) return respond(200, payload());

  if (typeof body.removeProject === "string") {
    projects = projects.filter(function (p) { return p.name !== body.removeProject; });
    delete checkCommands[body.removeProject];
    return respond(200, payload());
  }

  var fields = refuseNextSave || {};
  refuseNextSave = null;
  var known = projects.map(function (p) { return p.name; });
  Object.keys(body.checkCommands || {}).forEach(function (name) {
    // settings.py:439 -- validated against projects.json, not against
    // whatever the form happened to send.
    if (known.indexOf(name) === -1) fields["checkCommands." + name] = "unknown project";
  });
  if (Object.keys(fields).length) {
    return respond(400, {
      error: "invalid settings: " + Object.keys(fields).map(function (k) {
        return k + ": " + fields[k];
      }).join(", "),
      fields: fields
    });
  }
  Object.keys(body.checkCommands || {}).forEach(function (name) {
    checkCommands[name] = body.checkCommands[name];
  });
  return respond(200, payload());
};

// -- reading the rendered form --
function byId(id) { return document.getElementById(id); }
function checkCommandRows() {
  return byId("settings-check-commands").querySelectorAll("input[data-project]")
    .map(function (input) { return input.getAttribute("data-project"); });
}
function checkCommandValues() {
  var out = {};
  byId("settings-check-commands").querySelectorAll("input[data-project]").forEach(function (input) {
    out[input.getAttribute("data-project")] = input.value;
  });
  return out;
}
function projectRows() {
  return byId("settings-projects-list").querySelectorAll(".settings-project-name")
    .map(function (el) { return el.textContent; });
}
function removeButtons() {
  return byId("settings-projects-list").querySelectorAll("button");
}
function visibleFieldErrors() {
  return document.querySelectorAll("#settings-modal .settings-field-error")
    .filter(function (el) { return !el.hidden; })
    .map(function (el) { return el.textContent; });
}
function status() {
  return { text: byId("settings-status").textContent, className: byId("settings-status").className };
}
function lastSave() {
  for (var i = requests.length - 1; i >= 0; i--) {
    if (requests[i].body && requests[i].body.checkCommands) return requests[i].body;
  }
  return null;
}

var out = {};

// The board has seen a third project at some point in this session's
// life -- knownProjects never forgets one. Nothing the form renders may
// come from here.
C.knownProjects.length = 0;
["alpha", "beta", "ghost"].forEach(function (n) { C.knownProjects.push(n); });

byId("settings-open").click();
settle().then(function () {
  out.onOpen = { rows: checkCommandRows(), values: checkCommandValues(), projects: projectRows() };

  // Remove beta: first click arms the button, second confirms.
  removeButtons()[1].click();
  out.armed = removeButtons().map(function (b) { return b.textContent; });
  removeButtons()[1].click();
  return settle();
}).then(function () {
  out.afterRemove = {
    rows: checkCommandRows(),
    values: checkCommandValues(),
    projects: projectRows(),
    refreshes: refreshes,
    toasts: toasts.map(function (t) { return t.text; }),
    status: status()
  };

  byId("settings-save").click();
  return settle();
}).then(function () {
  out.afterSave = {
    status: status(),
    body: lastSave(),
    rows: checkCommandRows(),
    fieldErrors: visibleFieldErrors()
  };

  // Now the legibility half, independent of the bug above: a refusal
  // naming a field this form is not showing at all.
  refuseNextSave = { "checkCommands.ghost": "unknown project" };
  byId("settings-save").click();
  return settle();
}).then(function () {
  out.invisibleFieldRefusal = { status: status(), fieldErrors: visibleFieldErrors() };

  // ...and one that names both a field on screen and one that is not.
  refuseNextSave = {
    "refreshIntervalSeconds": "must be a whole number of seconds, at least 5",
    "checkCommands.ghost": "unknown project"
  };
  byId("settings-save").click();
  return settle();
}).then(function () {
  out.mixedRefusal = { status: status(), fieldErrors: visibleFieldErrors() };

  // A refusal every field of which IS on screen still reads the way it
  // always did.
  refuseNextSave = { "refreshIntervalSeconds": "must be a whole number of seconds, at least 5" };
  byId("settings-save").click();
  return settle();
}).then(function () {
  out.visibleFieldRefusal = { status: status(), fieldErrors: visibleFieldErrors() };

  // The row that IS on screen still shows its own error under itself.
  refuseNextSave = { "checkCommands.alpha": "must be a single command line" };
  byId("settings-save").click();
  return settle();
}).then(function () {
  out.rowRefusal = { status: status(), fieldErrors: visibleFieldErrors() };
  out.requests = requests.map(function (r) { return r.method + " " + r.url; });
  process.stdout.write(JSON.stringify(out));
}).catch(function (err) {
  process.stderr.write((err && err.stack) || String(err));
  process.exit(1);
});
"""


@js_harness.requires_node
class SettingsProjectRemovalBehaviourTests(unittest.TestCase):
    """Task-158: removing a project used to leave its check-command row
    behind, and the next Save was then refused with a message pointing
    at no visible field -- stuck until a page reload.

    Every assertion here is driven, because none of them could be made
    by reading the source: the rows were built by a loop that reads
    correctly line by line, over a list that happened to be the wrong
    one."""

    @property
    def out(self):
        return js_harness.cached_driver(self, SETTINGS_PROJECT_REMOVAL_DRIVER_JS)

    def test_the_rows_come_from_the_payload_and_not_from_the_board(self):
        # `knownProjects` carries a third name the settings payload does
        # not. If the render consulted it, "ghost" would have a row.
        self.assertEqual(self.out["onOpen"]["rows"], ["alpha", "beta"])
        self.assertEqual(self.out["onOpen"]["projects"], ["alpha", "beta"])
        self.assertEqual(self.out["onOpen"]["values"],
                         {"alpha": "make test", "beta": "pytest -q"})

    def test_removing_a_project_takes_its_check_command_row_with_it(self):
        # The bug, exactly: the projects list was right and the rows
        # were not.
        after = self.out["afterRemove"]
        self.assertEqual(after["projects"], ["alpha"])
        self.assertEqual(after["rows"], ["alpha"])
        self.assertEqual(after["values"], {"alpha": "make test"})

    def test_the_row_goes_without_waiting_on_the_board_refetch(self):
        # doRefresh() is still called -- the cards have to disappear --
        # but the form is correct before it could possibly have landed,
        # which is the whole point: it is rendered from the response the
        # removal itself returned.
        self.assertEqual(self.out["afterRemove"]["refreshes"], 1)
        self.assertEqual(self.out["afterRemove"]["toasts"], ['Project "beta" removed.'])
        self.assertEqual(self.out["afterRemove"]["status"]["text"], "Removed beta.")

    def test_saving_straight_after_a_removal_succeeds(self):
        # No reload in between, and no mention of the removed project in
        # what was sent -- so nothing for the server to refuse.
        self.assertEqual(self.out["afterSave"]["status"]["text"], "Saved.")
        self.assertEqual(self.out["afterSave"]["status"]["className"], "success")
        self.assertEqual(self.out["afterSave"]["body"]["checkCommands"], {"alpha": "make test"})
        self.assertEqual(self.out["afterSave"]["fieldErrors"], [])
        self.assertEqual(self.out["afterSave"]["rows"], ["alpha"])

    def test_the_removal_and_the_save_are_the_only_requests_made(self):
        # One GET to open, one POST to remove, then the saves. No
        # refetch of the settings was needed to get the rows right.
        self.assertEqual(self.out["requests"][:3],
                         ["GET /api/settings", "POST /api/settings", "POST /api/settings"])

    def test_a_refusal_naming_no_visible_field_still_says_what_happened(self):
        # The other half of the original failure: "Fix the highlighted
        # field and try again" highlighting nothing is indistinguishable
        # from a bug. Name the field, the reason, and the way out.
        refusal = self.out["invisibleFieldRefusal"]
        self.assertEqual(refusal["fieldErrors"], [])
        self.assertEqual(
            refusal["status"]["text"],
            "The server refused checkCommands.ghost (unknown project) -- that field is "
            "not on this form, so reload the page and try again.")
        self.assertEqual(refusal["status"]["className"], "error")

    def test_a_mixed_refusal_highlights_what_it_can_and_spells_out_the_rest(self):
        mixed = self.out["mixedRefusal"]
        self.assertEqual(mixed["fieldErrors"],
                         ["must be a whole number of seconds, at least 5"])
        self.assertEqual(
            mixed["status"]["text"],
            "Fix the highlighted field and try again. "
            "The server refused checkCommands.ghost (unknown project) -- that field is "
            "not on this form, so reload the page and try again.")

    def test_a_refusal_whose_fields_are_all_on_screen_reads_as_it_always_did(self):
        visible = self.out["visibleFieldRefusal"]
        self.assertEqual(visible["status"]["text"], "Fix the highlighted field and try again.")
        self.assertEqual(visible["fieldErrors"],
                         ["must be a whole number of seconds, at least 5"])

    def test_a_check_command_error_still_lands_under_its_own_row(self):
        row = self.out["rowRefusal"]
        self.assertEqual(row["fieldErrors"], ["must be a single command line"])
        self.assertEqual(row["status"]["text"], "Fix the highlighted field and try again.")

# ---------------------------------------------------------------------

# The other direction of task-158's bug, driven the way it was found
# (task-159): one project on the board, ADD a second, and try to give
# the new one a check command without reloading the page.
#
# Before task-158's fix the rows were built from `C.knownProjects`, so
# this direction failed SILENTLY: the new project really was added, the
# Projects list showed it, no check-command row appeared for it, and the
# next Save succeeded -- there was simply nothing to save, and nothing
# said so. (The removal direction at least failed loudly, which is why
# it was the half that got noticed first.)
#
# The driver puts the fix under exactly that load. `C.doRefresh` is a
# counter and nothing else, so `C.knownProjects` NEVER learns the new
# name: the board refetch the page asks for after an add cannot land
# here at all. A row for "gamma" therefore has only one possible source,
# the add response the form was rendered from -- which is the claim.
#
# The fake server mirrors settings.py: an add appends the project with
# no check command, and a save naming a project projects.json does not
# have is refused with `fields["checkCommands.<name>"] = "unknown
# project"`. `AddThenConfigureReachesProjectsJsonTests` below closes the
# last leg by replaying this driver's own two request bodies through the
# real `settings.apply_settings`.
SETTINGS_PROJECT_ADDITION_DRIVER_JS = js_harness.LOAD_SOURCES_JS + r"""
// The driver owns timers: nothing here waits on one, and the confirm-arm
// disarm timer must not hold node's loop open.
var armed = [];
global.setTimeout = function (fn, ms) { armed.push({ fn: fn, ms: ms }); return armed.length; };

var C = loadFrontend(["state.js", "dom.js", "tasks.js", "settings.js"]);
wireSettingsShell();

// The files this driver does not load, as sinks.
var toasts = [];
var refreshes = 0;
C.showToast = function (text, kind) { toasts.push({ text: text, kind: kind }); };
// Deliberately inert. The page asks for a board refresh after an add so
// the new project's column appears; here it only counts, so
// C.knownProjects never learns "gamma" and cannot be the source of any
// row on the form.
C.doRefresh = function () { refreshes++; };
C.syncDrawerPanePolling = function () {};

// The path typed into the add-project field. A real git repo when the
// caller passed one (the projects.json replay does, so settings.py's
// own add validation runs against something that exists); otherwise a
// placeholder, because this fake server never looks at it.
var ADD_PATH = process.argv[2] || "/repos/gamma";

// -- the fake /api/settings --
var projects = [{ name: "alpha", path: "/repos/alpha" }];
var checkCommands = { alpha: "echo alpha-tests" };
var requests = [];

function payload() {
  var commands = {};
  projects.forEach(function (p) { commands[p.name] = checkCommands[p.name] || null; });
  return {
    harvestMode: "click",
    sessionPreviewMode: "view",
    refreshIntervalSeconds: 30,
    checkCommands: commands,
    defaultAgent: "claude",
    agents: ["claude"],
    agentEntries: [{ name: "claude", cmdText: "claude", promptSuffix: "", builtin: true, onPath: true }],
    projects: projects.map(function (p) { return { name: p.name, path: p.path }; })
  };
}

function respond(status, data) {
  return Promise.resolve({
    ok: status < 400, status: status,
    json: function () { return Promise.resolve(data); }
  });
}

global.fetch = function (url, opts) {
  var body = opts && opts.body ? JSON.parse(opts.body) : null;
  requests.push({ url: url, method: (opts && opts.method) || "GET", body: body });
  if (!body) return respond(200, payload());

  if (body.addProject) {
    // settings.py:_write_whitelisted_changes -- a new entry is {name,
    // path} and nothing else, so the project starts with no check
    // command at all.
    projects.push({ name: body.addProject.name, path: body.addProject.path });
    return respond(200, payload());
  }

  var known = projects.map(function (p) { return p.name; });
  var fields = {};
  Object.keys(body.checkCommands || {}).forEach(function (name) {
    // settings.py:439 -- validated against projects.json, not against
    // whatever the form happened to send.
    if (known.indexOf(name) === -1) fields["checkCommands." + name] = "unknown project";
  });
  if (Object.keys(fields).length) {
    return respond(400, {
      error: "invalid settings: " + Object.keys(fields).map(function (k) {
        return k + ": " + fields[k];
      }).join(", "),
      fields: fields
    });
  }
  Object.keys(body.checkCommands || {}).forEach(function (name) {
    checkCommands[name] = body.checkCommands[name];
  });
  return respond(200, payload());
};

// -- reading the rendered form --
function byId(id) { return document.getElementById(id); }
function checkCommandInputs() {
  return byId("settings-check-commands").querySelectorAll("input[data-project]");
}
function checkCommandRows() {
  return checkCommandInputs().map(function (input) { return input.getAttribute("data-project"); });
}
function checkCommandValues() {
  var out = {};
  checkCommandInputs().forEach(function (input) {
    out[input.getAttribute("data-project")] = input.value;
  });
  return out;
}
function rowInput(name) {
  var hit = null;
  checkCommandInputs().forEach(function (input) {
    if (input.getAttribute("data-project") === name) hit = input;
  });
  return hit;
}
function projectRows() {
  return byId("settings-projects-list").querySelectorAll(".settings-project-name")
    .map(function (el) { return el.textContent; });
}
function visibleFieldErrors() {
  return document.querySelectorAll("#settings-modal .settings-field-error")
    .filter(function (el) { return !el.hidden; })
    .map(function (el) { return el.textContent; });
}
function status() {
  return { text: byId("settings-status").textContent, className: byId("settings-status").className };
}
function addStatus() {
  var el = byId("settings-add-project-status");
  return { text: el.textContent, className: el.className };
}
function bodyMatching(pick) {
  for (var i = requests.length - 1; i >= 0; i--) {
    if (requests[i].body && pick(requests[i].body)) return requests[i].body;
  }
  return null;
}

var out = {};

// Everything the board has ever seen. It does NOT contain "gamma" now
// and never will in this run -- see C.doRefresh above.
C.knownProjects.length = 0;
C.knownProjects.push("alpha");

byId("settings-open").click();
settle().then(function () {
  out.onOpen = { rows: checkCommandRows(), values: checkCommandValues(), projects: projectRows() };

  byId("settings-add-project-name").value = "gamma";
  byId("settings-add-project-path").value = ADD_PATH;
  byId("settings-add-project-init").checked = false;
  byId("settings-add-project-btn").click();
  return settle();
}).then(function () {
  out.afterAdd = {
    rows: checkCommandRows(),
    values: checkCommandValues(),
    projects: projectRows(),
    knownProjects: C.knownProjects.slice(),
    refreshes: refreshes,
    addStatus: addStatus(),
    toasts: toasts.map(function (t) { return t.text; }),
    fieldErrors: visibleFieldErrors(),
    addFields: {
      name: byId("settings-add-project-name").value,
      path: byId("settings-add-project-path").value,
      init: !!byId("settings-add-project-init").checked
    }
  };

  // Configure the new project without a reload: type into the row that
  // just appeared and Save. `typedInto` records that there WAS a row to
  // type into, so a driver run against the pre-fix source says which
  // step it lost rather than dying on a null.
  var input = rowInput("gamma");
  out.typedInto = !!input;
  if (input) input.value = "echo gamma-tests";
  byId("settings-save").click();
  return settle();
}).then(function () {
  out.afterSave = {
    status: status(),
    rows: checkCommandRows(),
    values: checkCommandValues(),
    fieldErrors: visibleFieldErrors(),
    stored: checkCommands
  };
  out.addBody = bodyMatching(function (b) { return !!b.addProject; });
  out.saveBody = bodyMatching(function (b) { return !!b.checkCommands; });
  out.requests = requests.map(function (r) { return r.method + " " + r.url; });
  process.stdout.write(JSON.stringify(out));
}).catch(function (err) {
  process.stderr.write((err && err.stack) || String(err));
  process.exit(1);
});
"""


@js_harness.requires_node
class SettingsProjectAdditionBehaviourTests(unittest.TestCase):
    """Task-159: adding a project used to leave the check-command rows
    unchanged, so the new project could not be given a test command
    until the page was reloaded -- and the Save that saved nothing said
    "Saved.", so nothing anywhere reported it.

    Task-158 fixed the shared root cause (the rows now come from the
    settings payload, never from the board's append-only
    `C.knownProjects`) and shipped the remove-then-save test. This is
    the add-then-configure half: the direction that failed silently is
    the direction that most needs a test holding it down."""

    @property
    def out(self):
        return js_harness.cached_driver(self, SETTINGS_PROJECT_ADDITION_DRIVER_JS)

    def test_the_form_opens_on_the_one_project_it_has(self):
        self.assertEqual(self.out["onOpen"]["rows"], ["alpha"])
        self.assertEqual(self.out["onOpen"]["projects"], ["alpha"])
        self.assertEqual(self.out["onOpen"]["values"], {"alpha": "echo alpha-tests"})

    def test_adding_a_project_brings_its_check_command_row_with_it(self):
        # The bug, exactly: the Projects list gained "gamma" and the
        # rows did not.
        after = self.out["afterAdd"]
        self.assertEqual(after["projects"], ["alpha", "gamma"])
        self.assertEqual(after["rows"], ["alpha", "gamma"])
        self.assertEqual(after["values"], {"alpha": "echo alpha-tests", "gamma": ""})
        self.assertEqual(after["fieldErrors"], [])

    def test_the_row_appears_without_a_reload_and_without_the_board(self):
        # `knownProjects` is the board's append-only list of every
        # project name ever seen, and in this run it never hears about
        # "gamma" at all -- the refresh the page asks for is a counter
        # here. So the row above cannot have come from anywhere but the
        # add response, and no reload happened either: this is one
        # continuously open modal.
        after = self.out["afterAdd"]
        self.assertIn("gamma", after["rows"])
        self.assertEqual(after["knownProjects"], ["alpha"])
        self.assertEqual(after["refreshes"], 1)
        self.assertEqual(after["toasts"], ['Project "gamma" added.'])
        self.assertEqual(after["addStatus"]["text"], 'Added "gamma".')
        self.assertEqual(after["addStatus"]["className"], "settings-hint success")

    def test_the_add_fields_are_cleared_for_the_next_one(self):
        self.assertEqual(self.out["afterAdd"]["addFields"],
                         {"name": "", "path": "", "init": False})

    def test_a_command_typed_into_the_new_row_is_what_gets_saved(self):
        # The silent failure was here: with no row there was nothing to
        # type into, so the save carried no command for the new project
        # and still reported success.
        self.assertTrue(self.out["typedInto"])
        self.assertEqual(self.out["saveBody"]["checkCommands"],
                         {"alpha": "echo alpha-tests", "gamma": "echo gamma-tests"})

    def test_the_save_succeeds_and_the_form_shows_what_was_stored(self):
        after = self.out["afterSave"]
        self.assertEqual(after["status"]["text"], "Saved.")
        self.assertEqual(after["status"]["className"], "success")
        self.assertEqual(after["fieldErrors"], [])
        self.assertEqual(after["rows"], ["alpha", "gamma"])
        self.assertEqual(after["values"],
                         {"alpha": "echo alpha-tests", "gamma": "echo gamma-tests"})
        self.assertEqual(after["stored"],
                         {"alpha": "echo alpha-tests", "gamma": "echo gamma-tests"})

    def test_the_add_and_the_save_are_the_only_requests_made(self):
        # One GET to open, one POST to add, one POST to save. No refetch
        # of the settings was needed to get the new row on screen.
        self.assertEqual(self.out["requests"],
                         ["GET /api/settings", "POST /api/settings", "POST /api/settings"])


@js_harness.requires_node
class AddThenConfigureReachesProjectsJsonTests(unittest.TestCase):
    """The last leg of the same flow, in Python: the two request bodies
    the page above actually sent, applied by the real
    `settings.apply_settings` to a temporary projects.json.

    The driver's fake server mirrors settings.py, which is exactly the
    kind of mirror that can drift; this replays the same bytes through
    the real thing, so "the command reaches projects.json for the new
    project" is a claim about the file rather than about the fake. The
    driver is re-run here (rather than shared with the class above) so
    the path it types into the add-project field is a real repo --
    settings.py refuses one that is not a git checkout, and mirroring
    that refusal away would have been the drift this test exists to
    catch."""

    def _repo(self):
        """A directory that passes settings.py's add-project checks: it
        exists, `run_git` is mocked to call it a work tree, and it
        already has backlog/config.yml so no `backlog init` is even
        considered."""
        root = tempfile.mkdtemp(prefix="centrale-task159-")
        self.addCleanup(shutil.rmtree, root, True)
        os.makedirs(os.path.join(root, "backlog"))
        with open(os.path.join(root, "backlog", "config.yml"), "w", encoding="utf-8") as f:
            f.write("project_name: gamma\n")
        return root

    def test_the_command_typed_into_the_new_row_lands_in_projects_json(self):
        repo = self._repo()
        out = js_harness.run_driver(self, SETTINGS_PROJECT_ADDITION_DRIVER_JS, repo)
        self.assertEqual(out["addBody"]["addProject"]["path"], repo)

        config_path = os.path.join(repo, "projects.json")
        on_disk = {
            "port": 7420,
            "projects": [{"name": "alpha", "path": "~/x/alpha", "checkCommand": "echo alpha-tests"}],
        }
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(on_disk, f)
        config = {
            "port": 0,
            "worktreeRoot": "/tmp/does-not-matter",
            "projects": [{"name": "alpha", "path": "~/x/alpha", "checkCommand": "echo alpha-tests"}],
            "harvest": {"mode": "click"},
            "refreshIntervalSeconds": 30,
            "agents": {"claude": {"cmd": ["claude"]}},
            "defaultAgent": "claude",
        }

        with mock.patch.object(server, "run_git",
                               return_value=mock.Mock(returncode=0, stdout="true\n", stderr="")):
            settings.apply_settings(config, out["addBody"], path=config_path)
        settings.apply_settings(config, out["saveBody"], path=config_path)

        with open(config_path, encoding="utf-8") as f:
            written = json.load(f)
        by_name = {p["name"]: p for p in written["projects"]}
        self.assertEqual(sorted(by_name), ["alpha", "gamma"])
        # .get, not [...]: the pre-fix failure was an ABSENT command, and
        # "None != 'echo gamma-tests'" is that story -- a KeyError would
        # only say the test broke.
        self.assertEqual(by_name["gamma"].get("checkCommand"), "echo gamma-tests")
        self.assertEqual(by_name["gamma"]["path"], repo)
        # The project that was already there is untouched by either body.
        self.assertEqual(by_name["alpha"]["checkCommand"], "echo alpha-tests")
        self.assertEqual(by_name["alpha"]["path"], "~/x/alpha")
        self.assertEqual(written["port"], 7420)


# ---------------------------------------------------------------------

# task-167: the sole configured project, driven the way a stranger hits
# it. The Remove button next to the only project on the board was
# disabled outright, titled "Can't remove the last project" -- so a
# first project added with the wrong path could not be taken back out
# of the UI at all, and hand-editing projects.json was the only way
# forward. Nothing here can be read off the source: whether the button
# ends up disabled is the product of the in-flight flag, the confirm
# arm/disarm re-render and the payload the removal itself returns.
#
# The fake server mirrors settings.py after this task: a removal is
# refused only for a reason about work it would strand (a live session,
# an unmerged spawn branch), never for how many projects remain.
SETTINGS_SOLE_PROJECT_DRIVER_JS = js_harness.LOAD_SOURCES_JS + r"""
// The driver owns timers: the confirm-arm disarm timer must not hold
// node's loop open, and nothing here waits on one.
var armed = [];
global.setTimeout = function (fn, ms) { armed.push({ fn: fn, ms: ms }); return armed.length; };

var C = loadFrontend(["state.js", "dom.js", "tasks.js", "settings.js"]);
wireSettingsShell();

var toasts = [];
var refreshes = 0;
C.showToast = function (text, kind) { toasts.push({ text: text, kind: kind }); };
C.doRefresh = function () { refreshes++; };
C.syncDrawerPanePolling = function () {};

// -- the fake /api/settings -- a first run: nothing configured yet.
var projects = [];
var checkCommands = {};
var refuseRemoval = null;   // a reason string, to force the strand guard

function payload() {
  var commands = {};
  projects.forEach(function (p) { commands[p.name] = checkCommands[p.name] || null; });
  return {
    harvestMode: "click",
    sessionPreviewMode: "view",
    refreshIntervalSeconds: 30,
    checkCommands: commands,
    defaultAgent: "claude",
    agents: ["claude"],
    agentEntries: [{ name: "claude", cmdText: "claude", promptSuffix: "", builtin: true, onPath: true }],
    projects: projects.map(function (p) { return { name: p.name, path: p.path }; })
  };
}

function respond(status, data) {
  return Promise.resolve({
    ok: status < 400, status: status,
    json: function () { return Promise.resolve(data); }
  });
}

global.fetch = function (url, opts) {
  var body = opts && opts.body ? JSON.parse(opts.body) : null;
  if (!body) return respond(200, payload());
  if (body.addProject) {
    projects.push({ name: body.addProject.name, path: body.addProject.path });
    checkCommands[body.addProject.name] = null;
    return respond(200, payload());
  }
  if (typeof body.removeProject === "string") {
    if (refuseRemoval) {
      var fields = { removeProject: refuseRemoval };
      refuseRemoval = null;
      return respond(400, {
        error: "invalid settings: removeProject: " + fields.removeProject,
        fields: fields
      });
    }
    projects = projects.filter(function (p) { return p.name !== body.removeProject; });
    delete checkCommands[body.removeProject];
    return respond(200, payload());
  }
  return respond(200, payload());
};

function byId(id) { return document.getElementById(id); }
function removeButtons() {
  return byId("settings-projects-list").querySelectorAll("button");
}
function buttonState() {
  return removeButtons().map(function (b) {
    return { text: b.textContent, disabled: b.disabled, title: b.title };
  });
}
function projectRows() {
  return byId("settings-projects-list").querySelectorAll(".settings-project-name")
    .map(function (el) { return el.textContent; });
}
function checkCommandRows() {
  return byId("settings-check-commands").querySelectorAll("input[data-project]")
    .map(function (input) { return input.getAttribute("data-project"); });
}
function status() {
  return { text: byId("settings-status").textContent, className: byId("settings-status").className };
}

var out = {};

byId("settings-open").click();
settle().then(function () {
  out.onOpen = { projects: projectRows(), buttons: buttonState() };

  // Add the first project, the way the empty state points a stranger at.
  byId("settings-add-project-name").value = "solo";
  byId("settings-add-project-path").value = "/repos/solo";
  byId("settings-add-project-btn").click();
  return settle();
}).then(function () {
  out.afterAdd = {
    projects: projectRows(),
    buttons: buttonState(),
    addStatus: byId("settings-add-project-status").textContent
  };

  // The strand guard refuses the next removal. The reason is the whole
  // value of that refusal, so it has to reach the user intact.
  refuseRemoval = "a live agent session is still running in this project "
    + "(centrale-solo-task-9) -- end it from the board first, or removing the "
    + "project leaves it running with nothing here able to reach it";
  removeButtons()[0].click();   // arm
  removeButtons()[0].click();   // confirm
  return settle();
}).then(function () {
  out.afterRefusal = {
    projects: projectRows(),
    buttons: buttonState(),
    status: status()
  };

  // ...and the same button, clicked again once the session is gone,
  // empties the board.
  removeButtons()[0].click();
  removeButtons()[0].click();
  return settle();
}).then(function () {
  out.afterRemove = {
    projects: projectRows(),
    rows: checkCommandRows(),
    buttons: buttonState(),
    status: status(),
    toasts: toasts.map(function (t) { return t.text; }),
    refreshes: refreshes
  };
  process.stdout.write(JSON.stringify(out));
}).catch(function (err) {
  process.stderr.write((err && err.stack) || String(err));
  process.exit(1);
});
"""


@js_harness.requires_node
class SettingsSoleProjectRemovalBehaviourTests(unittest.TestCase):
    """Task-167: the only configured project is removable, and a refusal
    to remove one says what is in the way."""

    @property
    def out(self):
        return js_harness.cached_driver(self, SETTINGS_SOLE_PROJECT_DRIVER_JS)

    def test_the_first_run_starts_with_no_projects_and_no_remove_buttons(self):
        self.assertEqual(self.out["onOpen"]["projects"], [])
        self.assertEqual(self.out["onOpen"]["buttons"], [])

    def test_the_only_project_still_gets_a_live_remove_button(self):
        # Two bugs meet on this one assertion. `var isLast =
        # projects.length <= 1` disabled the button permanently, with a
        # title that named arithmetic as the reason; and the add's own
        # render ran while settingsProjectActionInFlight was still true,
        # so even without that rule the new row's button came out
        # disabled and nothing re-enabled it until the modal was closed
        # and reopened. Add-then-remove is the first-run path.
        self.assertEqual(self.out["afterAdd"]["addStatus"], 'Added "solo".')
        self.assertEqual(self.out["afterAdd"]["projects"], ["solo"])
        self.assertEqual(self.out["afterAdd"]["buttons"], [{
            "text": "Remove",
            "disabled": False,
            "title": "Removes this project from the dashboard's config only -- "
                     "the repo itself is never touched.",
        }])

    def test_a_refused_removal_shows_the_reason_and_leaves_the_button_usable(self):
        # A 400 from the strand guard must arrive as the sentence naming
        # the session, not as the "invalid settings: ..." envelope, and
        # must not leave the row stuck mid-flight.
        after = self.out["afterRefusal"]
        self.assertEqual(after["projects"], ["solo"])
        self.assertEqual(
            after["status"]["text"],
            "Could not remove solo: a live agent session is still running in this "
            "project (centrale-solo-task-9) -- end it from the board first, or "
            "removing the project leaves it running with nothing here able to reach it")
        self.assertEqual(after["status"]["className"], "error")
        self.assertFalse(after["buttons"][0]["disabled"])

    def test_removing_it_empties_the_projects_list_and_its_check_command_row(self):
        after = self.out["afterRemove"]
        self.assertEqual(after["projects"], [])
        self.assertEqual(after["rows"], [])
        self.assertEqual(after["buttons"], [])
        self.assertEqual(after["status"]["text"], "Removed solo.")
        self.assertEqual(after["toasts"],
                         ['Project "solo" added.', 'Project "solo" removed.'])
        self.assertEqual(after["refreshes"], 2)  # one for the add, one for the removal


# ---------------------------------------------------------------------

# task-167, the sidebar half: `C.knownProjects` is "every project name
# ever seen" and only ever grew, so a project removed in Settings kept
# its chip and its place in the projects count for the rest of the
# session. Removing the LAST one -- the path this task exists for --
# therefore produced a board showing its first-run welcome panel beside
# a sidebar still listing the project that had just gone.
#
# Driven through the real `C.doRefresh`, because the claim is about what
# a board load does, and the third refresh below is a FAILED one: the
# prune must follow the payload, and a payload that never arrived is not
# a payload saying zero projects.
SIDEBAR_AFTER_REMOVAL_DRIVER_JS = js_harness.LOAD_SOURCES_JS + r"""
// api.js arms its countdown at load; nothing here waits on a timer.
global.setInterval = function () { return {}; };
global.setTimeout = function () { return {}; };
global.clearTimeout = function () {};

var C = loadFrontend(["state.js", "dom.js", "api.js", "tasks.js", "board.js"]);

// The files this driver does not load, as sinks.
C.renderSessionsPanel = function () {};
C.renderMilestoneFilter = function () {};
C.renderMilestoneChipLabel = function () {};
C.renderVersion = function () {};
C.renderCodeDrift = function () {};
C.showToast = function () {};
C.renderBoardAndSessionsIfChanged = function () {};
C.refreshDrawerDetail = function () {};

function byId(id) { return document.getElementById(id); }

var STATUSES = ["To Do", "In Progress", "Done"];
function project(name) {
  return { name: name, statuses: STATUSES, tasks: [] };
}

var boardResponse = { projects: [project("solo")], capabilities: { tmux: true } };
var boardFails = false;

global.fetch = function (url) {
  if (url.indexOf("/api/sessions") === 0) {
    return Promise.resolve({ ok: true, status: 200,
      json: function () { return Promise.resolve({ sessions: [] }); } });
  }
  if (boardFails) {
    return Promise.resolve({ ok: false, status: 500,
      json: function () { return Promise.resolve({ error: "board unavailable" }); } });
  }
  return Promise.resolve({ ok: true, status: 200,
    json: function () { return Promise.resolve(boardResponse); } });
};

// What main.js's renderAll would do with the two renders this driver
// actually loaded.
function renderAll() {
  C.renderProjectChips();
  C.renderBoard();
  return {
    chips: byId("project-chips").querySelectorAll(".sidebar-empty, .project-chip")
      .map(function (el) { return el.className === "sidebar-empty" ? "(empty)" : el.textContent; }),
    count: byId("projects-count").textContent,
    known: C.knownProjects.slice(),
    active: Array.from(C.activeProjects || []).sort(),
    emptyHintShown: byId("empty-hint").hidden === false,
    boardShown: byId("board").hidden === false
  };
}

var out = {};

C.doRefresh(true);
settle().then(function () {
  out.withProject = renderAll();

  // Removed in Settings: the next board load simply does not carry it.
  boardResponse = { projects: [], capabilities: { tmux: true } };
  C.doRefresh(true);
  return settle();
}).then(function () {
  out.afterRemoval = renderAll();

  // A failed refresh is not a payload saying "no projects".
  boardFails = true;
  boardResponse = { projects: [project("solo")], capabilities: { tmux: true } };
  C.doRefresh(true);
  return settle();
}).then(function () {
  boardFails = false;
  C.doRefresh(true);
  return settle();
}).then(function () {
  out.afterFailedThenGoodRefresh = renderAll();
  process.stdout.write(JSON.stringify(out));
}).catch(function (err) {
  process.stderr.write((err && err.stack) || String(err));
  process.exit(1);
});
"""


@js_harness.requires_node
class SidebarAfterProjectRemovalBehaviourTests(unittest.TestCase):
    """Task-167: the sidebar follows the board payload, so a removed
    project leaves it."""

    @property
    def out(self):
        return js_harness.cached_driver(self, SIDEBAR_AFTER_REMOVAL_DRIVER_JS)

    def test_a_configured_project_gets_its_chip(self):
        before = self.out["withProject"]
        self.assertEqual(before["known"], ["solo"])
        self.assertEqual(before["count"], "1")
        self.assertEqual(before["active"], ["solo"])
        self.assertFalse(before["emptyHintShown"])

    def test_removing_the_last_project_empties_the_sidebar_too(self):
        # The bug: the welcome panel and a chip for the project that had
        # just been removed, on screen at the same time.
        after = self.out["afterRemoval"]
        self.assertEqual(after["known"], [])
        self.assertEqual(after["active"], [])
        self.assertEqual(after["count"], "0")
        self.assertEqual(after["chips"], ["(empty)"])
        self.assertTrue(after["emptyHintShown"])
        self.assertFalse(after["boardShown"])

    def test_a_failed_refresh_never_prunes_and_the_next_good_one_restores(self):
        # A dropped tick leaves C.boardData untouched and never reaches
        # the prune at all, so nothing vanishes for a network blip; the
        # next successful load brings the project back.
        after = self.out["afterFailedThenGoodRefresh"]
        self.assertEqual(after["known"], ["solo"])
        self.assertEqual(after["count"], "1")


if __name__ == "__main__":
    unittest.main()
