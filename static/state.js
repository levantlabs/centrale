// static/state.js -- shared board/session/UI state, and the localStorage view-state helpers
//
// First of the per-concern frontend files static/index.html loads, in
// the order listed there (task-89; before that, all of it was one
// 4,274-line static/app.js). They are plain script files, loaded by
// their own tags at the end of the body -- no build step, no bundler, no
// modules -- so the seam between them is one namespace object,
// window.Centrale, taken by each file as C.
//
// Two rules, and only these two:
//   1. A shared value that gets REASSIGNED lives on C itself
//      (C.boardData, C.currentDrawer, ...) and is read and written as
//      C.x in every file, this one included: a local alias would go
//      stale the moment another file assigned to it.
//   2. Everything else shared -- functions, constants, containers that
//      are only mutated in place -- stays an ordinary declaration and is
//      published at the end of its own file (C.h = h;). Other files
//      reach it as C.h, always late-bound, so no file has to be loaded
//      before another for a call to resolve.
// A name used only inside one file stays a plain local; promoting one is
// two lines (publish it, prefix the caller).
(function (C) {
  "use strict";

  // ------------------------------------------------------------------
  // State
  // ------------------------------------------------------------------

  // ------------------------------------------------------------------
  // View-state persistence (localStorage, centrale-prefixed, try/catch
  // wrapped like window.CentraleTheme above). Search text, the open
  // drawer, and toasts are deliberately NOT persisted -- ephemeral by
  // design. A storage failure (unavailable, quota, corrupt value) just
  // falls back to the same defaults a first-ever visit would get.
  // ------------------------------------------------------------------

  var VIEW_STATE_KEYS = {
    activeProjects: "centrale-active-projects",
    readyOnly: "centrale-ready-only",
    milestone: "centrale-milestone-filter",
    sidebarCollapsed: "centrale-sidebar-collapsed",
    drawerWide: "centrale-drawer-wide",
    theaterRailCollapsed: "centrale-theater-rail-collapsed",
    drawerSections: "centrale-drawer-sections"
  };

  // Array of project names to start active, or null if nothing usable is
  // stored. An empty array is stored and read back as "no restriction"
  // (all active) -- see persistActiveProjects. Consumed once, by the
  // first registerKnownProjects() call, then left alone: a project that
  // shows up later in the session (added to projects.json mid-session)
  // should default to active, not be filtered by a now-stale snapshot.
  function readStoredActiveProjectNames() {
    try {
      var raw = window.localStorage.getItem(VIEW_STATE_KEYS.activeProjects);
      if (raw === null) return null;
      var arr = JSON.parse(raw);
      if (!Array.isArray(arr)) return null;
      return arr.filter(function (v) { return typeof v === "string"; });
    } catch (e) {
      return null;
    }
  }

  function persistActiveProjects() {
    try {
      // "All active" is stored as [] rather than omitting the key --
      // indistinguishable from "nothing stored yet" on the way back in
      // (both mean "start with everything active"), so this stays simple.
      var allActive = !C.activeProjects || knownProjects.length === 0 || C.activeProjects.size === knownProjects.length;
      var toStore = allActive ? [] : Array.from(C.activeProjects);
      window.localStorage.setItem(VIEW_STATE_KEYS.activeProjects, JSON.stringify(toStore));
    } catch (e) { /* storage unavailable: selection still applied in-memory */ }
  }

  function readStoredReadyOnly() {
    try {
      return window.localStorage.getItem(VIEW_STATE_KEYS.readyOnly) === "1";
    } catch (e) {
      return false;
    }
  }

  function persistReadyOnly(value) {
    try {
      window.localStorage.setItem(VIEW_STATE_KEYS.readyOnly, value ? "1" : "0");
    } catch (e) { /* storage unavailable: filter still applied in-memory */ }
  }

  // task-86: the selected milestone, or "" for the "All milestones"
  // default. A value that no longer exists on the board is deliberately
  // kept rather than reset, so the choice stays visible in the dropdown
  // instead of silently emptying the lanes; see renderMilestoneFilter.
  //
  // task-91: what is stored is the project-qualified key tasks.js's
  // milestoneKey() builds ("my-lib/m-0"), not the bare "m-0" id this used
  // to hold -- ids repeat across repos, so the id alone selected two
  // unrelated milestones at once. A stored value from before that fix
  // has no "/" and is dropped here rather than restored as a selection
  // matching nothing: the only thing mirrored from tasks.js is the
  // presence of the separator, never how either half is encoded.
  function readStoredMilestoneFilter() {
    try {
      var v = window.localStorage.getItem(VIEW_STATE_KEYS.milestone);
      return typeof v === "string" && v.indexOf("/") >= 0 ? v : "";
    } catch (e) {
      return "";
    }
  }

  function persistMilestoneFilter(value) {
    try {
      window.localStorage.setItem(VIEW_STATE_KEYS.milestone, value || "");
    } catch (e) { /* storage unavailable: filter still applied in-memory */ }
  }

  // null = no explicit user choice stored -- the viewport heuristic at
  // boot should decide instead. "1"/"0" (an explicit choice, made via
  // the sidebar-toggle button or the Ctrl/Cmd+K auto-expand) always wins
  // over the viewport from then on.
  function readStoredSidebarCollapsed() {
    try {
      var v = window.localStorage.getItem(VIEW_STATE_KEYS.sidebarCollapsed);
      return (v === "1" || v === "0") ? (v === "1") : null;
    } catch (e) {
      return null;
    }
  }

  function persistSidebarCollapsed(value) {
    try {
      window.localStorage.setItem(VIEW_STATE_KEYS.sidebarCollapsed, value ? "1" : "0");
    } catch (e) { /* storage unavailable: choice still applied in-memory */ }
  }

  // task-68: the drawer's wide mode (see applyDrawerWidth). A per-viewer
  // convenience like the others here -- never product config, so never
  // projects.json. Anything but a stored "1" (nothing stored, "0", a
  // corrupt value, storage unavailable) is the default summary width.
  function readStoredDrawerWide() {
    try {
      return window.localStorage.getItem(VIEW_STATE_KEYS.drawerWide) === "1";
    } catch (e) {
      return false;
    }
  }

  function persistDrawerWide(value) {
    try {
      window.localStorage.setItem(VIEW_STATE_KEYS.drawerWide, value ? "1" : "0");
    } catch (e) { /* storage unavailable: width still applied in-memory */ }
  }

  // task-93: the session theater's task rail, collapsed or not (see
  // applyTheaterRail). Same shape and the same per-viewer rules as
  // readStoredDrawerWide above -- never product config, so never
  // projects.json. Anything but a stored "1" (nothing stored, "0", a
  // corrupt value, storage unavailable) means the rail is shown, which
  // is the default the feature ships with.
  function readStoredTheaterRailCollapsed() {
    try {
      return window.localStorage.getItem(VIEW_STATE_KEYS.theaterRailCollapsed) === "1";
    } catch (e) {
      return false;
    }
  }

  function persistTheaterRailCollapsed(value) {
    try {
      window.localStorage.setItem(VIEW_STATE_KEYS.theaterRailCollapsed, value ? "1" : "0");
    } catch (e) { /* storage unavailable: choice still applied in-memory */ }
  }

  // task-97: which drawer sections the viewer has explicitly folded or
  // unfolded (see drawerSection in drawer.js). A map of section key ->
  // boolean, holding ONLY the sections actually toggled: an absent key
  // means "use the size-derived default", so a fresh viewer still gets
  // a 200-word description folded and a two-line one open. Same
  // per-viewer-convenience rules as the two preferences above -- never
  // product config, so never projects.json.
  function readStoredDrawerSections() {
    try {
      var raw = window.localStorage.getItem(VIEW_STATE_KEYS.drawerSections);
      if (raw === null) return {};
      var parsed = JSON.parse(raw);
      if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return {};
      var clean = {};
      for (var k in parsed) {
        if (Object.prototype.hasOwnProperty.call(parsed, k) && typeof parsed[k] === "boolean") {
          clean[k] = parsed[k];
        }
      }
      return clean;
    } catch (e) {
      return {};
    }
  }

  function persistDrawerSectionCollapsed(key, collapsed) {
    C.drawerSectionCollapsed[key] = !!collapsed;
    try {
      window.localStorage.setItem(
        VIEW_STATE_KEYS.drawerSections, JSON.stringify(C.drawerSectionCollapsed)
      );
    } catch (e) { /* storage unavailable: choice still applied in-memory */ }
  }

  C.boardData = { projects: [] };
  C.sessionsData = [];
  var knownProjects = [];      // ordered list of project names ever seen
  C.activeProjects = null;     // Set of project names currently toggled on (null = uninitialized)
  C.storedActiveProjectNames = readStoredActiveProjectNames(); // consumed once, see above
  C.readyOnly = readStoredReadyOnly();
  C.milestoneFilter = readStoredMilestoneFilter(); // task-86: "" = all milestones
  C.drawerWide = readStoredDrawerWide(); // task-68: preference; applied only while the pane section exists
  C.theaterRailCollapsed = readStoredTheaterRailCollapsed(); // task-93: preference; applied only while the theater rail exists
  C.drawerSectionCollapsed = readStoredDrawerSections(); // task-97: per-section overrides of the size-derived collapse default
  C.searchText = "";
  C.refreshIntervalSeconds = 10; // updated from /api/board's refreshIntervalSeconds each load
  C.sessionPreviewMode = "view"; // "off" | "view" | "interact"; updated from /api/board's sessionPreviewMode each load (task-60/61). Starts at the read-only tier so no reply row can appear before the server has said it's allowed.
  C.countdownSeconds = 10;
  C.lastBoardError = null;
  C.firstLoadDone = false;

  // task-50: JSON snapshot of {board, sessions, error} as of the last
  // fetch-driven render -- lets renderBoardAndSessionsIfChanged() (see
  // below) skip the whole rebuild when the freshly fetched auto-refresh
  // payload is byte-identical to what's already on screen (the common
  // case: nothing changed). Verified against the real server that a
  // force-recomputed /api/board, and /api/sessions, are both exactly
  // byte-identical JSON across repeated fetches with no real change in
  // between -- no volatile/noisy fields (timestamps, etc.) had to be
  // excluded from this comparison.
  C.lastRenderedFetchPayload = null;
  // task-86: signature of the milestone dropdown's last rendered option
  // list + selection -- see renderMilestoneFilter.
  C.lastMilestoneFilterSignature = null;

  C.currentDrawer = null;      // { project, id, summary, branchTask } -- branchTask is set once
                                // renderDrawerDetail's /api/task fetch resolves (task-38 needs it
                                // for the "likely finished" fallback), null/undefined until then.
  C.drawerRequestSeq = 0;
  // task-126: the drawer's body is re-fetched on every refresh tick for
  // as long as the drawer is open, so both of these are about telling
  // one /api/task response from another (see fetchDrawerDetail).
  //   payload      -- JSON of the response the body on screen was built
  //                   from, so a tick that brings back exactly the same
  //                   bytes can return without touching any DOM.
  //   inFlightSeq  -- the drawerRequestSeq a request now in flight was
  //                   sent under, or 0 when nothing is out: one request
  //                   at a time, and a superseded response can never
  //                   clear the marker a newer one set.
  C.drawerDetailPayload = null;
  C.drawerDetailInFlightSeq = 0;

  var spawnStates = {};        // key "project::taskId" -> {status, error, attach, session}
  var spawnConfirmPending = {}; // key "project::taskId" -> {expires: epochMs, action, count}
                                // task-116: `action` is "spawn" | "respawn" | "resume" -- the three
                                // share this one entry per task, and the drawer can now show two of
                                // them at once, so arming one must not arm the other (spawn.armedFor).
  var SPAWN_CONFIRM_WINDOW_MS = 5000;

  var harvestStates = {};      // key "project::taskId" -> {status: 'loading'|'success'|'blocked'|'error', report, error, progressLabel}
  // TASK-64's destructive/authoritative harvest actions each get their
  // own informed-confirm state; neither can accidentally arm the other.
  var adoptDoneConfirmPending = {};     // key -> {expires}
  var reconcileConfirmPending = {};     // key -> {expires}  (task-66 "Resume to reconcile")
  var discardMainConfirmPending = {};   // key -> {expires}
  C.harvestAllInFlight = false;

  var endSessionStates = {};   // key "project::taskId" -> {status: 'loading'|'success'|'error', error}
  // task-132: End session on a WORKING agent is the two-click arm every
  // other consequential action here uses, not a disabled button. Its
  // own map, like the rest: arming it must never light up a spawn or
  // cleanup confirm on the same task, and the drawer's button and the
  // sidebar row's share this one entry, so arming either shows in both.
  var endSessionConfirmPending = {}; // key "project::taskId" -> {expires: epochMs}

  // task-43: cleanup of a branch already fully merged (Centrale's own
  // harvest, or entirely out-of-band -- see server.py's alreadyMerged).
  // cleanupConfirmPending shares the informed-confirm shape task-40
  // introduced for spawnConfirmPending, but its own map: a dirty
  // worktree's cleanup requires one arm-then-confirm click, a clean
  // one is a single click, and this must never interact with a
  // concurrent spawn/respawn/resume confirm on the same task.
  var cleanupStates = {};         // key "project::taskId" -> {status: 'loading'|'success'|'error', discardedPaths, branch, error}
  var cleanupConfirmPending = {}; // key "project::taskId" -> {expires: epochMs}

  // task-119: throwing a bad attempt away. Two actions that differ ONLY
  // in whether the branch survives -- discard removes the worktree and
  // deletes the branch so the next spawn branches fresh from the base;
  // abandon removes only the worktree and leaves the branch parked --
  // so they get two states and two confirm maps rather than one shared
  // pair with a flag. Arming "abandon" must never light up "discard".
  //
  // The pending entry carries the `preview` GET /api/discard-preview
  // measured at the arming click, because the confirming label is built
  // from it: the second click has to name the same "3 commits and 4
  // uncommitted files" the user just read, not a number re-fetched
  // underneath them.
  var discardStates = {};          // key -> {status: 'previewing'|'loading'|'success'|'error', branch,
                                   //         branchTip, commitCount, discardedPaths, recoveryTag,
                                   //         recoveryCommand, error}
  var discardConfirmPending = {};  // key -> {expires: epochMs, preview}
  var abandonStates = {};          // key -> {status: 'previewing'|'loading'|'success'|'error', branch,
                                   //         commitCount, discardedPaths, error}
  var abandonConfirmPending = {};  // key -> {expires: epochMs, preview}

  // project name -> epoch-seconds of the newest harvest event we've already
  // shown (or, on the first poll for that project, silently seeded so we
  // never replay history from before auto mode was noticed).
  var autoHarvestEventWatermarks = {};

  var CANONICAL_STATUSES = ["To Do", "In Progress", "Done"];

  // ------------------------------------------------------------------
  // Seam: what the other files reach for through window.Centrale.
  // ------------------------------------------------------------------

  C.CANONICAL_STATUSES = CANONICAL_STATUSES;
  C.SPAWN_CONFIRM_WINDOW_MS = SPAWN_CONFIRM_WINDOW_MS;
  C.abandonConfirmPending = abandonConfirmPending;
  C.abandonStates = abandonStates;
  C.adoptDoneConfirmPending = adoptDoneConfirmPending;
  C.autoHarvestEventWatermarks = autoHarvestEventWatermarks;
  C.cleanupConfirmPending = cleanupConfirmPending;
  C.cleanupStates = cleanupStates;
  C.discardMainConfirmPending = discardMainConfirmPending;
  C.discardConfirmPending = discardConfirmPending;
  C.discardStates = discardStates;
  C.endSessionConfirmPending = endSessionConfirmPending;
  C.endSessionStates = endSessionStates;
  C.harvestStates = harvestStates;
  C.knownProjects = knownProjects;
  C.persistActiveProjects = persistActiveProjects;
  C.persistDrawerSectionCollapsed = persistDrawerSectionCollapsed;
  C.persistDrawerWide = persistDrawerWide;
  C.persistTheaterRailCollapsed = persistTheaterRailCollapsed;
  C.persistMilestoneFilter = persistMilestoneFilter;
  C.persistReadyOnly = persistReadyOnly;
  C.persistSidebarCollapsed = persistSidebarCollapsed;
  C.readStoredSidebarCollapsed = readStoredSidebarCollapsed;
  C.reconcileConfirmPending = reconcileConfirmPending;
  C.spawnConfirmPending = spawnConfirmPending;
  C.spawnStates = spawnStates;
})(window.Centrale = window.Centrale || {});
