// static/api.js -- /api fetches, auto-harvest event toasts, and the refresh/countdown loop
//
// One of the per-concern frontend files loaded in order by
// static/index.html (task-89); the seam between them is the single
// window.Centrale namespace object, taken here as C. See
// static/state.js for the two rules that seam follows.
(function (C) {
  "use strict";

  // ------------------------------------------------------------------
  // Fetch helpers
  // ------------------------------------------------------------------

  function fetchBoard(force) {
    var url = "/api/board" + (force ? "?force=1" : "");
    return fetch(url).then(function (res) {
      return res.json().catch(function () { return null; }).then(function (data) {
        if (!res.ok) {
          var msg = (data && data.error) ? data.error : ("Failed to load board (HTTP " + res.status + ")");
          throw new Error(msg);
        }
        return data;
      });
    }).then(function (data) {
      C.boardData = (data && Array.isArray(data.projects)) ? data : { projects: [] };
      if (typeof C.boardData.refreshIntervalSeconds === "number" && C.boardData.refreshIntervalSeconds >= 1) {
        C.refreshIntervalSeconds = C.boardData.refreshIntervalSeconds;
      }
      if (typeof C.boardData.sessionPreviewMode === "string") {
        C.sessionPreviewMode = C.boardData.sessionPreviewMode;
      }
      C.lastBoardError = null;
      C.renderVersion(); // task-107: settles on the first load, no-ops after
      C.renderCodeDrift(); // task-128: follows every load -- the checkout can move at any time
      registerKnownProjects();
      C.firstLoadDone = true;
      C.renderBoardAndSessionsIfChanged();
      pollAutoHarvestEventsIfEnabled();
    }).catch(function (err) {
      C.lastBoardError = err.message || String(err);
      C.firstLoadDone = true;
      C.renderBoardAndSessionsIfChanged();
    });
  }

  // ------------------------------------------------------------------
  // Auto-harvest event toasts
  // ------------------------------------------------------------------
  //
  // When harvest.mode is "auto" the server merges ready branches in the
  // background on its own schedule, without any click. Those outcomes are
  // otherwise invisible in the UI, so once the board reports auto mode we
  // poll GET /api/harvest per known project on the normal 10s refresh
  // cadence and toast anything new since the last poll. Click-triggered
  // harvests already surface inline via harvestTask()'s own response, so
  // only "auto"-triggered events are toasted here to avoid double-reporting.

  function pollAutoHarvestEventsIfEnabled() {
    if (!C.boardData || C.boardData.harvestMode !== "auto") return;
    C.knownProjects.forEach(pollAutoHarvestEventsForProject);
  }

  function pollAutoHarvestEventsForProject(projectName) {
    fetch("/api/harvest?project=" + encodeURIComponent(projectName)).then(function (res) {
      return res.json().catch(function () { return null; }).then(function (data) {
        if (!res.ok) throw new Error((data && data.error) || "Failed to load harvest events");
        return data;
      });
    }).then(function (data) {
      var events = (data && Array.isArray(data.events)) ? data.events : [];
      var watermark = C.autoHarvestEventWatermarks[projectName];
      if (watermark === undefined) {
        // First time we've polled this project: seed the watermark to the
        // newest event without toasting -- those happened before we were
        // watching (or before auto mode was turned on).
        var newest = events.reduce(function (max, e) { return Math.max(max, e.time || 0); }, 0);
        C.autoHarvestEventWatermarks[projectName] = newest;
        return;
      }
      var newEvents = events.filter(function (e) {
        return e.trigger === "auto" && (e.time || 0) > watermark;
      });
      newEvents.sort(function (a, b) { return (a.time || 0) - (b.time || 0); });
      var anyMerged = false;
      newEvents.forEach(function (e) {
        if (e.merged) {
          anyMerged = true;
          // Same instant-feedback mechanism as a click: mark the task's
          // harvestState now, so its card/drawer transition immediately
          // rather than waiting on the doRefresh(true) below (still
          // called, for everything else that changed).
          if (e.taskId) {
            C.harvestStates[C.spawnKey(projectName, e.taskId)] = { status: "success", report: e };
          }
          C.showToast("Auto-merged " + (e.branch || e.taskId || "a branch") + " into " + (e.baseBranch || "main") + ".", "success");
        } else if (e.error) {
          C.showToast("Auto-merge attempt failed for " + (e.branch || e.taskId || "a branch") + ": " + e.error, "error");
        }
        // Gate-blocked auto attempts (not merged, no error) are routine --
        // most branches aren't ready most cycles -- so they're left out of
        // the toast stream to avoid noise; they're still in the event log.
      });
      var newestSeen = events.reduce(function (max, e) { return Math.max(max, e.time || 0); }, watermark);
      C.autoHarvestEventWatermarks[projectName] = newestSeen;
      if (anyMerged) {
        C.renderBoard(); // instant, using the harvestStates just recorded above
        if (C.currentDrawer) { C.renderDrawerSpawnArea(); C.renderDrawerHarvestArea(); C.renderDrawerSessionArea(); }
        doRefresh(true); // and still refetch, for everything else that changed
      }
    }).catch(function () {
      // Auto-harvest event polling is best-effort; a failed poll just
      // means we try again on the next 10s refresh.
    });
  }

  function fetchSessions() {
    return fetch("/api/sessions").then(function (res) {
      return res.json().catch(function () { return null; }).then(function (data) {
        if (!res.ok) throw new Error((data && data.error) || "Failed to load sessions");
        return data;
      });
    }).then(function (data) {
      C.sessionsData = (data && Array.isArray(data.sessions)) ? data.sessions : [];
      // spawn-disabled state on cards, and the sessions panel itself,
      // may have changed -- renderBoardAndSessionsIfChanged() covers
      // both (and skips entirely if nothing actually did).
      C.renderBoardAndSessionsIfChanged();
    }).catch(function () {
      // Sessions failures are non-fatal; keep the last known list.
    });
  }

  function registerKnownProjects() {
    if (C.activeProjects === null) C.activeProjects = new Set();
    // A non-empty stored selection restricts which of THIS FIRST batch of
    // projects start active; a stored name not present here (stale, since
    // dropped from projects.json) just never matches, so it's silently
    // dropped. Consumed once -- see storedActiveProjectNames above.
    var useStored = !!C.storedActiveProjectNames && C.storedActiveProjectNames.length > 0;
    (C.boardData.projects || []).forEach(function (p) {
      if (C.knownProjects.indexOf(p.name) === -1) {
        C.knownProjects.push(p.name);
        if (!useStored || C.storedActiveProjectNames.indexOf(p.name) !== -1) {
          C.activeProjects.add(p.name); // default to active, or explicitly restored active
        }
      }
    });
    C.storedActiveProjectNames = null;

    // ...and drop the ones that are no longer configured (task-167).
    // This list only ever grew, so removing a project left its chip in
    // the sidebar and its name in the projects count -- and removing the
    // LAST one left the board showing its first-run welcome panel beside
    // a sidebar still listing the project that had just gone. Same
    // conclusion task-158 reached for the settings modal's rows: the
    // board payload carries every configured project, error banner and
    // all, so it is the only thing that knows which they are. Safe here
    // because this runs on a SUCCESSFUL load only -- a failed fetch
    // leaves C.boardData untouched and never reaches this function, so a
    // dropped network tick can never empty the sidebar.
    var live = {};
    (C.boardData.projects || []).forEach(function (p) { live[p.name] = true; });
    for (var i = C.knownProjects.length - 1; i >= 0; i--) {
      var name = C.knownProjects[i];
      if (!live[name]) {
        C.knownProjects.splice(i, 1);
        C.activeProjects.delete(name);
      }
    }
  }

  // ------------------------------------------------------------------
  // Refresh / countdown loop
  // ------------------------------------------------------------------

  function doRefresh(force) {
    fetchBoard(!!force);
    fetchSessions();
    // task-126: and the open drawer's BODY, which /api/board does not
    // carry -- one extra GET /api/task per tick, for one task, and only
    // while a drawer is actually open (the call returns before it
    // fetches otherwise). No timer of its own: it rides this one.
    C.refreshDrawerDetail();
    C.countdownSeconds = C.refreshIntervalSeconds;
    updateCountdownDisplay();
  }

  function updateCountdownDisplay() {
    C.byId("countdown").textContent = "next refresh in " + C.countdownSeconds + "s";
    var lu = C.byId("last-updated");
    if (C.firstLoadDone) {
      var now = new Date();
      lu.textContent = "updated " + now.toLocaleTimeString();
    }
  }

  setInterval(function () {
    C.countdownSeconds -= 1;
    if (C.countdownSeconds <= 0) {
      doRefresh(false);
    } else {
      updateCountdownDisplay();
    }
    C.updateDrawerPaneAge(); // task-60: the pane's "Ns ago" label ticks on the same 1s clock
  }, 1000);

  // ------------------------------------------------------------------
  // Seam: what the other files reach for through window.Centrale.
  // ------------------------------------------------------------------

  C.doRefresh = doRefresh;
  C.fetchSessions = fetchSessions;
})(window.Centrale = window.Centrale || {});
