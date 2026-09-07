// static/harvest.js -- the gated merge, the post-merge branch cleanup and End session
//
// One of the per-concern frontend files loaded in order by
// static/index.html (task-89); the seam between them is the single
// window.Centrale namespace object, taken here as C. See
// static/state.js for the two rules that seam follows.
(function (C) {
  "use strict";

  // ------------------------------------------------------------------
  // Harvest (gated auto-merge)
  // ------------------------------------------------------------------

  // task.hasSpawnBranch is only as fresh as the last board fetch. Right
  // after a merge succeeds (single click, part of "Merge all", or an
  // auto-merge event), harvestStates already knows the branch is gone --
  // this makes the card/drawer transition instantly, in the same render
  // pass, rather than waiting on the board's next refetch to catch up
  // (doRefresh(true) is still called on every merge path, to pick up
  // everything else that changed, but the button itself shouldn't have
  // to wait for it). A successful cleanup (task-43) removes the branch
  // exactly the same way a merge does, so cleanupStates gets the same
  // instant-hide treatment here.
  function effectiveHasSpawnBranch(task, projectName, taskId) {
    if (!task.hasSpawnBranch) return false;
    var key = C.spawnKey(projectName, taskId);
    var hState = C.harvestStates[key];
    if (hState && hState.status === "success") return false;
    var cState = C.cleanupStates[key];
    if (cState && cState.status === "success") return false;
    // task-119: a discard deletes the branch outright, so the same
    // instant transition applies -- and here it hands the task BACK to
    // the ordinary Spawn button rather than taking it away, which is
    // the whole point of discarding an attempt.
    var dState = C.discardStates[key];
    if (dState && dState.status === "success") return false;
    return true;
  }

  // task-94: "derive, don't remember". A remembered gate verdict is a
  // POST response, not something re-evaluated on render, and nothing
  // else ever removes one -- so a blocked/error verdict outlived the
  // branch state it described: Merge (blocked) -> Resume to reconcile
  // -> End session brought the same red "merge conflict" line back,
  // describing a conflict the agent had already resolved. It only
  // *looked* like it came back because renderDrawerHarvestArea bails
  // out entirely while a session is live; ending the session lifted
  // that suppression and re-rendered the pre-resume verdict.
  //
  // Both moments where the branch is about to move on, or already has,
  // drop the stale verdict: a successful spawn/re-spawn/resume, and a
  // successful end-session. With nothing remembered, the drawer and the
  // card fall back to a plain Merge button -- honest, since Centrale
  // genuinely does not know the gate outcome until it asks again, and
  // the user's next click asks. Deliberately no re-evaluation here: no
  // GET /api/harvest, no request of any kind, purely client-side.
  //
  // ONLY "blocked" and "error" are dropped. A "success" verdict must
  // survive exactly as it does today -- the post-merge confirmation
  // line and the task-41 justMerged/justCleanedUp suppression of the
  // Spawn button both read it through the stale-until-refetch window.
  // A "loading" entry is a POST still in flight, which overwrites
  // itself when it lands; dropping it would only lose the "Merging…"
  // button state.
  function invalidateStaleMergeVerdict(projectName, taskId) {
    var key = C.spawnKey(projectName, taskId);
    var state = C.harvestStates[key];
    if (!state) return false;
    if (state.status !== "blocked" && state.status !== "error") return false;
    delete C.harvestStates[key];
    return true;
  }

  function firstFailedGate(gates) {
    return (gates || []).filter(function (g) { return g.passed === false; })[0] || null;
  }

  function gateSummaryTooltip(gates) {
    return (gates || []).map(function (g) {
      var mark = g.passed === true ? "✓" : (g.passed === false ? "✗" : "–");
      return mark + " " + g.name + (g.reason ? ": " + g.reason : "");
    }).join("\n");
  }

  function refreshHarvestButtons(projectName, taskId) {
    C.renderBoard();
    if (C.currentDrawer && C.currentDrawer.project === projectName && C.currentDrawer.id === taskId) {
      C.renderDrawerHarvestArea();
      C.renderDrawerSessionArea();
    }
  }

  // ------------------------------------------------------------------
  // Live merge progress (task-96)
  // ------------------------------------------------------------------
  //
  // A merge is one POST that blocks for as long as the gates take, and on
  // a project with a checkCommand practically all of that is the test
  // suite -- seconds of a button reading "Merging…" with no way to tell a
  // running suite from a hung dashboard. GET /api/harvest-progress names
  // the gate the server is on RIGHT NOW; this polls it while our own POST
  // is still outstanding and relabels the button with that name.
  //
  // Two rules, both deliberate:
  //   * Nothing is ever inferred. The label comes only from a record the
  //     server published, matched to the merge this client asked for. No
  //     progress -- an early poll, a failed poll, someone else's harvest
  //     holding the merge lock, a stage name this frontend doesn't know
  //     -- means the plain "Merging…" of before, never a guess from the
  //     known gate order.
  //   * No time is shown anywhere: no elapsed count, estimate, countdown
  //     or percentage. WHICH gate is running is the whole point; how long
  //     it has taken, or might take, is not something Centrale can say
  //     usefully.
  // The first poll is one full interval in, so a project with no
  // checkCommand -- merged in well under half a second -- finishes before
  // any poll fires and never flashes a label.

  var MERGE_PROGRESS_URL = "/api/harvest-progress";
  var MERGE_PROGRESS_POLL_MS = 1000;

  // Server stage name -> button label. checkCommand is the one that
  // matters: it is where practically all of a merge's time goes.
  var MERGE_STAGE_LABELS = {
    noLiveSession: "Checking session…",
    taskDone: "Checking task…",
    worktreeClean: "Checking worktree…",
    mergeClean: "Trial merge…",
    mainCheckoutClean: "Checking checkout…",
    checkCommand: "Running tests…",
    merge: "Merging…"
  };

  // The label for what the server says it is doing, or null -- which
  // every caller renders as the plain fallback.
  function mergeProgressLabel(progress, matches) {
    if (!progress || !matches(progress)) return null;
    return MERGE_STAGE_LABELS[progress.gate] || null;
  }

  // One poll loop: reads the live record every interval, hands the
  // matching label to `apply`, and reschedules until the returned stop()
  // is called. Self-rescheduling rather than setInterval so a slow poll
  // can never stack up, and the first fetch is one interval in.
  function pollMergeProgress(matches, apply) {
    var stopped = false;
    var timer = null;

    function schedule() {
      if (stopped) return;
      timer = setTimeout(function () {
        timer = null;
        fetch(MERGE_PROGRESS_URL).then(function (res) {
          return res.ok ? res.json() : null;
        }).then(function (data) {
          if (!stopped) apply(mergeProgressLabel(data && data.progress, matches));
        }).catch(function () {
          if (!stopped) apply(null); // a failed poll falls back; it never guesses
        }).then(schedule);
      }, MERGE_PROGRESS_POLL_MS);
    }

    schedule();
    return function stop() {
      stopped = true;
      if (timer) { clearTimeout(timer); timer = null; }
    };
  }

  function harvestTask(projectName, taskId, action) {
    var key = C.spawnKey(projectName, taskId); // same "project::taskId" scheme
    var previousReport = C.harvestStates[key] && C.harvestStates[key].report;
    C.harvestStates[key] = {
      status: "loading", action: action || null, report: previousReport, progressLabel: null
    };
    refreshHarvestButtons(projectName, taskId);

    // task-96: only this client's own merge is polled for, and only for
    // as long as its POST is outstanding -- the record has to name this
    // exact project+task, so an auto-harvest cycle (or another browser)
    // holding the merge lock leaves the button on its plain fallback
    // rather than describing someone else's merge.
    var stopProgress = pollMergeProgress(function (p) {
      return p.project === projectName && p.taskId === taskId;
    }, function (label) {
      var live = C.harvestStates[key];
      if (!live || live.status !== "loading") return;
      if ((live.progressLabel || null) === label) return;
      live.progressLabel = label;
      refreshHarvestButtons(projectName, taskId);
    });

    var payload = { project: projectName, taskId: taskId };
    if (action === "adoptDone") payload.adoptDone = true;
    if (action === "discardMainTaskEdit") payload.discardMainTaskEdit = true;

    fetch("/api/harvest", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload)
    }).then(function (res) {
      return res.json().catch(function () { return {}; }).then(function (data) {
        if (!res.ok) throw new Error((data && data.error) || ("HTTP " + res.status));
        return data;
      });
    }).then(function (data) {
      if (data.alreadyMerged) {
        // Not a gate failure -- there was nothing here to evaluate at
        // all (already merged by this click racing another, or removed
        // by hand). Success-shaped: an info toast and a refetch, same
        // as a real merge, not the "blocked" gate-failure treatment.
        C.harvestStates[key] = { status: "success", report: data };
        C.showToast("Already merged — nothing to do here.", "success");
        C.doRefresh(true);
        return;
      }
      C.harvestStates[key] = { status: data.merged ? "success" : "blocked", report: data };
      if (Array.isArray(data.warnings)) {
        data.warnings.forEach(function (w) { C.showToast(w, "error"); });
      }
      if (data.merged) {
        C.doRefresh(true); // the branch/worktree are gone and the board changed
      }
    }).catch(function (err) {
      C.harvestStates[key] = {
        status: "error",
        error: err.message || String(err),
        report: previousReport
      };
    }).then(function () {
      stopProgress(); // the response has landed: nothing left to report on
      refreshHarvestButtons(projectName, taskId);
    });
  }

  var MERGE_TOOLTIP = "Merge this task's branch back into the base branch, if all safety gates pass";

  // task-81: a branch checked out outside Centrale (task.branchCheckout
  // kind "external") keeps its Merge button in place but DISABLED, with
  // the reason as its title -- the same idiom as the drawer's disabled
  // "Worked externally" action and the card's "Session live". Derived
  // from board data on every render, so it re-enables by itself on the
  // next refetch once the foreign worktree is gone; no client state.
  // Callers must not attach the harvest click handler when `external`
  // is set, so the disabled button can never POST /api/harvest.
  function harvestButtonDisplay(state, task) {
    if (state && state.status === "loading") {
      // task-96: the gate the server says it is on, or -- with nothing
      // published (yet, or at all) -- exactly today's plain "Merging…".
      return {
        text: state.progressLabel || "Merging…",
        disabled: true, title: MERGE_TOOLTIP, external: false
      };
    }
    var ext = C.externalCheckout(task);
    if (ext) return { text: "Merge", disabled: true, title: C.externalMergeReason(ext), external: true };
    return { text: "Merge", disabled: false, title: MERGE_TOOLTIP, external: false };
  }

  function harvestActionIsArmed(map, key) {
    var pending = map[key];
    if (pending && pending.expires > Date.now()) return true;
    if (pending) delete map[key];
    return false;
  }

  function armHarvestAction(map, projectName, taskId) {
    var key = C.spawnKey(projectName, taskId);
    map[key] = { expires: Date.now() + C.SPAWN_CONFIRM_WINDOW_MS };
    refreshHarvestButtons(projectName, taskId);
    setTimeout(function () {
      var current = map[key];
      if (current && current.expires <= Date.now()) {
        delete map[key];
        refreshHarvestButtons(projectName, taskId);
      }
    }, C.SPAWN_CONFIRM_WINDOW_MS + 100);
  }

  function handleAdoptDoneClick(projectName, taskId) {
    var key = C.spawnKey(projectName, taskId);
    if (harvestActionIsArmed(C.adoptDoneConfirmPending, key)) {
      delete C.adoptDoneConfirmPending[key];
      harvestTask(projectName, taskId, "adoptDone");
      return;
    }
    armHarvestAction(C.adoptDoneConfirmPending, projectName, taskId);
  }

  function adoptDoneButtonDisplay(projectName, taskId, state, divergence) {
    if (state && state.status === "loading") {
      return { text: "Adopting Done…", disabled: true, confirming: false };
    }
    var branchStatus = divergence.branchStatus || "unknown";
    var lastCommit = divergence.lastBranchCommitSubject || "(unavailable)";
    var detail = "Agent branch status: " + branchStatus +
      ". Last branch commit: “" + lastCommit +
      "”. Confirming sets the branch task to Done, commits that task edit, re-runs every gate, and merges only if green.";
    if (harvestActionIsArmed(C.adoptDoneConfirmPending, C.spawnKey(projectName, taskId))) {
      return {
        text: "Confirm adopt Done & merge?",
        title: detail,
        detail: detail,
        disabled: false,
        confirming: true
      };
    }
    return {
      text: "Adopt board Done onto branch & merge",
      title: "Review the agent branch status and last commit before adopting the board-side Done.",
      disabled: false,
      confirming: false
    };
  }

  function handleDiscardMainClick(projectName, taskId) {
    var key = C.spawnKey(projectName, taskId);
    if (harvestActionIsArmed(C.discardMainConfirmPending, key)) {
      delete C.discardMainConfirmPending[key];
      harvestTask(projectName, taskId, "discardMainTaskEdit");
      return;
    }
    armHarvestAction(C.discardMainConfirmPending, projectName, taskId);
  }

  function discardMainButtonDisplay(projectName, taskId, state, discardable) {
    if (state && state.status === "loading") {
      return { text: "Discarding board edit…", disabled: true, confirming: false };
    }
    var taskPath = discardable.path;
    var detail = "Discard exactly “" + taskPath +
      "” from the main checkout; then re-run every gate and merge only if green. No other path is touched.";
    if (harvestActionIsArmed(C.discardMainConfirmPending, C.spawnKey(projectName, taskId))) {
      return {
        text: "Confirm discard & merge?",
        title: detail,
        detail: detail,
        disabled: false,
        confirming: true
      };
    }
    return {
      text: "Discard redundant board edit & merge",
      title: "The branch task file supersedes the uncommitted board-side copy at " + taskPath,
      disabled: false,
      confirming: false
    };
  }

  // task-66: "Resume to reconcile". Offered ONLY when a Merge click came
  // back blocked at the merge/check gate with report.behindBase set
  // (the server sets it only when the branch is actually behind its
  // base -- see harvest._annotate_behind_base) and no session is live
  // (the harvest area is hidden entirely while one is). Same two-click
  // informed confirm as adopt/discard, its own pending map so it can't
  // arm those. Confirming posts the ordinary /api/resume with
  // {reconcile: true}: Centrale merges/rebases nothing itself -- the
  // agent merges <base> into the branch in its own worktree, and the
  // ordinary gates re-verify afterward.
  function handleReconcileClick(projectName, taskId) {
    if (!C.isTmuxAvailable()) return;
    var key = C.spawnKey(projectName, taskId);
    if (harvestActionIsArmed(C.reconcileConfirmPending, key)) {
      delete C.reconcileConfirmPending[key];
      C.resumeTask(projectName, taskId, true);
      return;
    }
    armHarvestAction(C.reconcileConfirmPending, projectName, taskId);
  }

  function reconcileButtonDisplay(projectName, taskId, behind) {
    var sState = C.spawnStates[C.spawnKey(projectName, taskId)];
    if (sState && sState.status === "loading") {
      return { text: "Resuming…", disabled: true, confirming: false, title: "" };
    }
    if (!C.isTmuxAvailable()) {
      return { text: "Resume to reconcile", disabled: true, confirming: false, title: C.TMUX_UNAVAILABLE_TOOLTIP };
    }
    var base = (behind && behind.baseBranch) || "main";
    var count = (behind && behind.count) || 0;
    var detail = "Sends this task's agent back into its existing worktree with a reconcile prompt: merge " +
      base + " (" + count + " newer commit" + (count === 1 ? "" : "s") + ") into this branch, resolve " +
      "textual and semantic conflicts, get the check command green, and commit on the branch. " +
      "Centrale itself merges nothing; the safety gates re-run afterward.";
    if (harvestActionIsArmed(C.reconcileConfirmPending, C.spawnKey(projectName, taskId))) {
      return { text: "Confirm resume to reconcile?", detail: detail, title: detail, disabled: false, confirming: true };
    }
    return {
      text: "Resume to reconcile",
      title: "This branch predates " + count + " newer commit" + (count === 1 ? "" : "s") + " on " + base +
        " -- the gate failure may be a collision with that newer work. Click to review what confirming does.",
      disabled: false,
      confirming: false
    };
  }

  function renderHarvestStatusLine(state) {
    if (state.status === "error") {
      var errLine = C.h("div", { className: "spawn-status-line error", text: state.error });
      errLine.addEventListener("click", function (e) { e.stopPropagation(); });
      return errLine;
    }
    if (state.status === "success") {
      var report = state.report || {};
      var text = report.alreadyMerged
        ? "Already merged — nothing to do here"
        : "Merged " + (report.branch || "") + " into " + (report.baseBranch || "main");
      if (report.unrelatedDirtyCount) {
        text += " (" + report.unrelatedDirtyCount + " unrelated uncommitted file" +
          (report.unrelatedDirtyCount === 1 ? "" : "s") + " left untouched)";
      }
      if (Array.isArray(report.discardedPaths) && report.discardedPaths.length) {
        text += " (discarded board-side edit: " + report.discardedPaths.join(", ") + ")";
      }
      var okLine = C.h("div", { className: "spawn-status-line success", text: text });
      okLine.addEventListener("click", function (e) { e.stopPropagation(); });
      return okLine;
    }
    // "blocked": at least one gate failed. Show the first failure reason
    // inline; the full per-gate breakdown is available on hover.
    var failed = firstFailedGate(state.report && state.report.gates);
    var blockedText = failed ? ("Not ready: " + failed.reason) : "Not ready to merge";
    var divergence = state.report && state.report.doneDivergence;
    if (divergence) {
      var mainLocation = "board/main checkout";
      if (divergence.mainTaskUncommitted && divergence.mainTaskPath) {
        mainLocation += " has an uncommitted edit at " + divergence.mainTaskPath + " and";
      }
      blockedText = "Not ready: agent branch status is " +
        (divergence.branchStatus || "unknown") + "; " + mainLocation +
        " status is " + (divergence.boardStatus || "unknown") + ".";
    }
    // task-66: the server attaches reconcileHint only when the failing
    // gate is mergeClean/checkCommand AND the branch is behind its base
    // -- an up-to-date failing branch never gets this sentence.
    var reconcileHint = state.report && state.report.reconcileHint;
    if (reconcileHint) {
      blockedText += " " + reconcileHint.charAt(0).toUpperCase() + reconcileHint.slice(1) + ".";
    }
    var blockedLine = C.h("div", {
      className: "spawn-status-line error",
      text: blockedText,
      title: gateSummaryTooltip(state.report && state.report.gates)
    });
    blockedLine.addEventListener("click", function (e) { e.stopPropagation(); });
    return blockedLine;
  }

  // "Harvest all ready" (sidebar): harvests every currently-ready
  // task/<id> branch across every configured project, one project at a
  // time (each project's own harvest-all call already re-evaluates and
  // harvests its branches one at a time server-side).
  function harvestAllReady() {
    if (C.harvestAllInFlight || C.knownProjects.length === 0) return;
    C.harvestAllInFlight = true;
    var btn = C.byId("harvest-all-btn");
    if (btn) { btn.disabled = true; btn.textContent = "Merging…"; }

    var totalMerged = 0;
    var errors = [];

    // task-96: the same live gate label as a single Merge, matched on
    // the project this pass is currently POSTing for -- "Merge all"
    // walks projects one at a time, and each project's harvest is what
    // the server is actually running while its request is outstanding.
    // A record for any other project (an auto-harvest cycle elsewhere)
    // leaves the button on its plain "Merging…".
    var currentProject = null;
    var allProgressLabel = null;
    var stopProgress = pollMergeProgress(function (p) {
      return p.project === currentProject;
    }, function (label) {
      if (label === allProgressLabel) return;
      allProgressLabel = label;
      if (btn) btn.textContent = label || "Merging…";
    });

    function next(i) {
      if (i >= C.knownProjects.length) {
        C.harvestAllInFlight = false;
        stopProgress();
        if (btn) { btn.disabled = false; btn.textContent = "Merge all ready"; }
        if (totalMerged > 0) {
          C.showToast("Merged " + totalMerged + " branch" + (totalMerged === 1 ? "" : "es") + ".", "success");
          C.renderBoard(); // instant, using the harvestStates just recorded above
          if (C.currentDrawer) { C.renderDrawerSpawnArea(); C.renderDrawerHarvestArea(); C.renderDrawerSessionArea(); }
          C.doRefresh(true); // and still refetch, for everything else that changed
        } else if (errors.length === 0) {
          C.showToast("Nothing ready to merge.", "success");
        }
        errors.forEach(function (e) { C.showToast(e, "error"); });
        return;
      }
      var projectName = C.knownProjects[i];
      currentProject = projectName;
      fetch("/api/harvest", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ project: projectName, all: true })
      }).then(function (res) {
        return res.json().catch(function () { return {}; }).then(function (data) {
          if (!res.ok) throw new Error((data && data.error) || ("HTTP " + res.status));
          return data;
        });
      }).then(function (data) {
        var mergedReports = data.merged || [];
        totalMerged += mergedReports.length;
        // Same instant-feedback mechanism as a single click: mark each
        // merged task's harvestState now, so its card/drawer transition
        // immediately rather than waiting on the doRefresh(true) below
        // (still called, for everything else that changed).
        mergedReports.forEach(function (report) {
          C.harvestStates[C.spawnKey(projectName, report.taskId)] = { status: "success", report: report };
        });
      }).catch(function (err) {
        errors.push(projectName + ": " + (err.message || err));
      }).then(function () {
        next(i + 1);
      });
    }
    next(0);
  }

  // ------------------------------------------------------------------
  // Cleanup of an already-merged branch (task-43)
  // ------------------------------------------------------------------
  //
  // task.alreadyMerged (server-computed: the branch tip is an ancestor
  // of the base branch) means Merge is the wrong offer here -- clicking
  // it would only ever refuse with "nothing to merge". This replaces it
  // with "Merged -- clean up", which calls POST /api/cleanup-branch: the
  // server re-verifies ancestry itself, removes the worktree (force),
  // and deletes the branch; main is never touched. Suppressed while a
  // session is live, same as Merge. A dirty worktree (task.worktreeDirty)
  // requires the same two-click informed confirm task-40 introduced for
  // spawning onto a claimed task -- untracked files can be real unsaved
  // work (the TASK-9 lesson) -- while a clean worktree cleans up in one
  // click. task-80: a branch whose checkout the board reports as
  // external (task-70) is never offered the click at all -- the card
  // footer and drawer render externalWorkButton instead, mirroring the
  // server's own 409 for that state.

  function cleanupBranch(projectName, taskId) {
    var key = C.spawnKey(projectName, taskId); // same "project::taskId" scheme
    C.cleanupStates[key] = { status: "loading" };
    refreshHarvestButtons(projectName, taskId);

    fetch("/api/cleanup-branch", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ project: projectName, taskId: taskId })
    }).then(function (res) {
      return res.json().catch(function () { return {}; }).then(function (data) {
        if (!res.ok) throw new Error((data && data.error) || ("HTTP " + res.status));
        return data;
      });
    }).then(function (data) {
      var discarded = data.discardedPaths || [];
      C.cleanupStates[key] = { status: "success", branch: data.branch, discardedPaths: discarded };
      var n = discarded.length;
      C.showToast(
        "Cleaned up " + (data.branch || "branch") +
          (n > 0 ? (" (" + n + " uncommitted file" + (n === 1 ? "" : "s") + " discarded)") : "") + ".",
        "success"
      );
      C.doRefresh(true); // the branch/worktree are gone and the board changed
    }).catch(function (err) {
      C.cleanupStates[key] = { status: "error", error: err.message || String(err) };
    }).then(function () {
      refreshHarvestButtons(projectName, taskId);
    });
  }

  // First click on a dirty worktree arms an informed confirm (the exact
  // file list is server-side only -- worktreeDirty is just a yes/no --
  // so, per task-43's design, the armed message stays count-free rather
  // than naming files the client doesn't have); a second click inside
  // the window proceeds. A clean worktree skips arming and cleans up on
  // the first click. Uses its own cleanupConfirmPending map, never
  // spawnConfirmPending -- Spawn/Respawn/Resume and this are mutually
  // exclusive states for a given task, but sharing one map would still
  // be a confusing false coupling between unrelated actions.
  function handleCleanupClick(projectName, taskId) {
    var key = C.spawnKey(projectName, taskId);
    var task = C.findTask(projectName, taskId);

    if (!task || !task.worktreeDirty) {
      cleanupBranch(projectName, taskId);
      return;
    }

    var pending = C.cleanupConfirmPending[key];
    if (pending && pending.expires > Date.now()) {
      delete C.cleanupConfirmPending[key];
      cleanupBranch(projectName, taskId);
      return;
    }

    C.cleanupConfirmPending[key] = { expires: Date.now() + C.SPAWN_CONFIRM_WINDOW_MS };
    refreshHarvestButtons(projectName, taskId);
    setTimeout(function () {
      var current = C.cleanupConfirmPending[key];
      if (current && current.expires <= Date.now()) {
        delete C.cleanupConfirmPending[key];
        refreshHarvestButtons(projectName, taskId);
      }
    }, C.SPAWN_CONFIRM_WINDOW_MS + 100);
  }

  function cleanupButtonDisplay(projectName, taskId, state) {
    if (state && state.status === "loading") return { text: "Cleaning up…", disabled: true, confirming: false };
    var pending = C.cleanupConfirmPending[C.spawnKey(projectName, taskId)];
    if (pending && pending.expires > Date.now()) {
      return {
        text: "Confirm cleanup?",
        confirmTitle: "Uncommitted changes in the worktree will be discarded — clean up anyway?",
        disabled: false,
        confirming: true
      };
    }
    return { text: "Merged — clean up", disabled: false, confirming: false };
  }

  function renderCleanupStatusLine(state) {
    if (state.status === "error") {
      var errLine = C.h("div", { className: "spawn-status-line error", text: "Failed to clean up: " + state.error });
      errLine.addEventListener("click", function (e) { e.stopPropagation(); });
      return errLine;
    }
    var n = (state.discardedPaths || []).length;
    var text = "Cleaned up " + (state.branch || "branch") +
      (n > 0 ? (" (" + n + " uncommitted file" + (n === 1 ? "" : "s") + " discarded)") : "");
    var okLine = C.h("div", { className: "spawn-status-line success", text: text });
    okLine.addEventListener("click", function (e) { e.stopPropagation(); });
    return okLine;
  }

  // ------------------------------------------------------------------
  // Throwing an attempt away (task-119)
  // ------------------------------------------------------------------
  //
  // "This attempt is bad, throw it away and let me start over." End
  // session only kills the agent; the cleanup above only handles
  // branches that are ALREADY merged. These two actions are the way
  // out of a spawn that produced nothing worth keeping:
  //
  //   Discard attempt -- POST /api/discard-attempt.
  //     Worktree removed AND branch deleted, so the task is left with
  //     no branch at all and the ordinary Spawn button comes back. The
  //     branch has to go, not just the worktree: spawn reuses an
  //     existing task/<id> branch, so parking it would make every
  //     re-spawn start from the same bad commits.
  //   Abandon worktree, keep branch -- POST /api/abandon-worktree.
  //     Only the worktree goes; the branch is left parked (task-70's
  //     "none" kind), still mergeable later without a worktree. Not
  //     merging now, but not throwing the work away either.
  //
  // Both are drawer-only, secondary, and take two clicks. The first
  // click MEASURES (GET /api/discard-preview) and the second destroys,
  // so the confirming label names what is actually about to go: "3
  // commits and 4 uncommitted files", never a generic "are you sure".
  // The measurement rides on the pending entry, so the click that
  // destroys names the same numbers the user just read rather than
  // re-fetching underneath them.
  //
  // Neither action touches the backlog task's status -- the board says
  // where a task stands, and that is the user's call, not a side effect
  // of throwing an attempt away.

  function countPhrase(n, singular) {
    return n + " " + singular + (n === 1 ? "" : "s");
  }

  function fetchDiscardPreview(projectName, taskId) {
    var url = "/api/discard-preview?project=" + encodeURIComponent(projectName) +
      "&task=" + encodeURIComponent(taskId);
    return fetch(url).then(function (res) {
      return res.json().catch(function () { return {}; }).then(function (data) {
        if (!res.ok) throw new Error((data && data.error) || ("HTTP " + res.status));
        return data;
      });
    });
  }

  // The two refusals the POSTs would make, read off the preview so the
  // arming click can say why instead of arming a button whose confirm
  // could only come back 409. Fresher than the board's own copy of
  // either fact, which is the point of measuring at click time.
  function previewRefusal(preview) {
    if (preview.liveSession) {
      return "A live session is still running: " + preview.liveSession +
        ". End it first.";
    }
    if (preview.externalCheckout) return preview.externalCheckout.reason;
    return null;
  }

  // null whenever a number is one the server could not measure. A null
  // count never becomes a comfortable-looking zero: the confirm refuses
  // to arm instead, since understating what is about to be destroyed is
  // the one thing this sentence exists to prevent.
  function discardConfirmText(preview) {
    if (preview.commitCount === null || preview.commitCount === undefined) return null;
    if (preview.dirtyFileCount === null || preview.dirtyFileCount === undefined) return null;
    return "Discard " + countPhrase(preview.commitCount, "commit") + " and " +
      countPhrase(preview.dirtyFileCount, "uncommitted file") + "?";
  }

  function discardConfirmDetail(preview) {
    return "Deletes " + preview.branch + " and removes " + preview.worktreePath +
      ". The " + countPhrase(preview.commitCount, "commit") + " over " + preview.baseBranch +
      " stay recoverable through a tag left at " + String(preview.branchTip || "").slice(0, 10) +
      "; the " + countPhrase(preview.dirtyFileCount, "uncommitted file") + " do not.";
  }

  function abandonConfirmText(preview) {
    if (preview.dirtyFileCount === null || preview.dirtyFileCount === undefined) return null;
    return "Remove the worktree and discard " +
      countPhrase(preview.dirtyFileCount, "uncommitted file") + "?";
  }

  function abandonConfirmDetail(preview) {
    var commits = (preview.commitCount === null || preview.commitCount === undefined)
      ? "Committed work"
      : "The " + countPhrase(preview.commitCount, "commit");
    return commits + " on " + preview.branch +
      " stays and can still be merged later; only the " +
      countPhrase(preview.dirtyFileCount, "uncommitted file") + " in " +
      preview.worktreePath + " are discarded.";
  }

  // Shared first click for both actions: measure, then arm. `states`
  // and `pending` are passed in rather than looked up by a kind string
  // so the two can never reach into each other's map -- the difference
  // between them is whether the branch survives, which is not a thing
  // to get wrong by typo.
  function armThrowawayAction(states, pending, projectName, taskId, confirmTextOf) {
    var key = C.spawnKey(projectName, taskId);
    states[key] = { status: "previewing" };
    refreshHarvestButtons(projectName, taskId);

    fetchDiscardPreview(projectName, taskId).then(function (preview) {
      var refusal = previewRefusal(preview);
      if (refusal) throw new Error(refusal);
      if (confirmTextOf(preview) === null) {
        throw new Error(
          "Centrale could not measure what this would destroy -- refusing to confirm blind.");
      }
      delete states[key];
      pending[key] = { expires: Date.now() + C.SPAWN_CONFIRM_WINDOW_MS, preview: preview };
      refreshHarvestButtons(projectName, taskId);
      setTimeout(function () {
        var current = pending[key];
        if (current && current.expires <= Date.now()) {
          delete pending[key];
          refreshHarvestButtons(projectName, taskId);
        }
      }, C.SPAWN_CONFIRM_WINDOW_MS + 100);
    }).catch(function (err) {
      delete pending[key];
      states[key] = { status: "error", error: err.message || String(err) };
      refreshHarvestButtons(projectName, taskId);
    });
  }

  function throwawayIsArmed(pending, key) {
    var entry = pending[key];
    if (entry && entry.expires > Date.now()) return entry;
    if (entry) delete pending[key];
    return null;
  }

  // `preview` is the measurement the armed confirm was built from, and
  // it rides along with the destructive click (task-121): the server
  // re-measures under its own lifecycle lock and refuses with 409 if the
  // branch tip or the uncommitted files have moved since, so a
  // repository that changed while the confirm sat armed costs a fresh
  // preview rather than destroying commits nobody was ever shown.
  function postThrowaway(url, states, projectName, taskId, preview, onSuccess) {
    var key = C.spawnKey(projectName, taskId);
    states[key] = { status: "loading" };
    refreshHarvestButtons(projectName, taskId);

    fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        project: projectName,
        taskId: taskId,
        expectedBranchTip: preview.branchTip,
        expectedDirtyPaths: preview.dirtyPaths || []
      })
    }).then(function (res) {
      return res.json().catch(function () { return {}; }).then(function (data) {
        if (!res.ok) throw new Error((data && data.error) || ("HTTP " + res.status));
        return data;
      });
    }).then(function (data) {
      states[key] = onSuccess(data);
      // task-94: whatever gate verdict is remembered describes a branch
      // that has just been thrown away or unmoored from its worktree.
      invalidateStaleMergeVerdict(projectName, taskId);
      C.doRefresh(true);
    }).catch(function (err) {
      // Includes the 409 a moved repository earns. The pending entry is
      // already gone by now, so the button is back to its unarmed label
      // and the only way on is another first click -- a fresh preview,
      // whose confirm names the numbers as they are now (task-121).
      states[key] = { status: "error", error: err.message || String(err) };
    }).then(function () {
      refreshHarvestButtons(projectName, taskId);
    });
  }

  function handleDiscardAttemptClick(projectName, taskId) {
    var key = C.spawnKey(projectName, taskId);
    var armedDiscard = throwawayIsArmed(C.discardConfirmPending, key);
    if (armedDiscard) {
      delete C.discardConfirmPending[key];
      postThrowaway("/api/discard-attempt", C.discardStates, projectName, taskId,
        armedDiscard.preview,
        function (data) {
          var discarded = data.discardedPaths || [];
          // The toast expires in six seconds, so it carries the whole
          // recovery command rather than a pointer to it; the drawer
          // line below keeps a copyable copy for as long as the drawer
          // is open.
          C.showToast(
            "Discarded " + data.branch + " (" + countPhrase(data.commitCount || 0, "commit") +
              ", " + countPhrase(discarded.length, "uncommitted file") + "). Recover: " +
              (data.recoveryCommand || "(no commits to recover)"),
            "success");
          return {
            status: "success",
            branch: data.branch,
            branchTip: data.branchTip,
            commitCount: data.commitCount,
            discardedPaths: discarded,
            recoveryTag: data.recoveryTag,
            recoveryCommand: data.recoveryCommand
          };
        });
      return;
    }
    armThrowawayAction(C.discardStates, C.discardConfirmPending,
      projectName, taskId, discardConfirmText);
  }

  function handleAbandonWorktreeClick(projectName, taskId) {
    var key = C.spawnKey(projectName, taskId);
    var armedAbandon = throwawayIsArmed(C.abandonConfirmPending, key);
    if (armedAbandon) {
      delete C.abandonConfirmPending[key];
      postThrowaway("/api/abandon-worktree", C.abandonStates, projectName, taskId,
        armedAbandon.preview,
        function (data) {
          var discarded = data.discardedPaths || [];
          C.showToast(
            "Removed the worktree for " + data.branch + " (" +
              countPhrase(discarded.length, "uncommitted file") + " discarded). The branch is parked.",
            "success");
          return {
            status: "success",
            branch: data.branch,
            commitCount: data.commitCount,
            discardedPaths: discarded
          };
        });
      return;
    }
    armThrowawayAction(C.abandonStates, C.abandonConfirmPending,
      projectName, taskId, abandonConfirmText);
  }

  function discardButtonDisplay(projectName, taskId, state) {
    if (state && state.status === "loading") {
      return { text: "Discarding…", disabled: true, confirming: false };
    }
    if (state && state.status === "previewing") {
      return { text: "Checking what would go…", disabled: true, confirming: false };
    }
    var armed = throwawayIsArmed(C.discardConfirmPending, C.spawnKey(projectName, taskId));
    if (armed) {
      var detail = discardConfirmDetail(armed.preview);
      return {
        text: discardConfirmText(armed.preview),
        title: detail,
        detail: detail,
        disabled: false,
        confirming: true
      };
    }
    // task-125: short at rest -- "Discard attempt" is enough for a pill
    // sitting in a row of them, and the full sentence has a better home
    // one click later, where the armed label already names the commits
    // and files that would go. The tooltip carries the rest meanwhile.
    return {
      text: "Discard attempt",
      title: "This attempt is bad: remove its worktree, delete its branch, and let the task be" +
        " spawned fresh from the base. The next click says exactly what would be destroyed.",
      disabled: false,
      confirming: false
    };
  }

  function abandonButtonDisplay(projectName, taskId, state) {
    if (state && state.status === "loading") {
      return { text: "Removing worktree…", disabled: true, confirming: false };
    }
    if (state && state.status === "previewing") {
      return { text: "Checking what would go…", disabled: true, confirming: false };
    }
    var armed = throwawayIsArmed(C.abandonConfirmPending, C.spawnKey(projectName, taskId));
    if (armed) {
      var detail = abandonConfirmDetail(armed.preview);
      return {
        text: abandonConfirmText(armed.preview),
        title: detail,
        detail: detail,
        disabled: false,
        confirming: true
      };
    }
    return {
      text: "Abandon worktree, keep branch",
      title: "Free the worktree but keep the commits: the branch stays parked and can still be" +
        " merged later. The next click says exactly what would be destroyed.",
      disabled: false,
      confirming: false
    };
  }

  function stopClick(node) {
    node.addEventListener("click", function (e) { e.stopPropagation(); });
    return node;
  }

  function renderDiscardStatusLine(state) {
    if (state.status === "error") {
      return stopClick(C.h("div", {
        className: "spawn-status-line error",
        text: "Discard refused: " + state.error
      }));
    }
    var commits = (state.commitCount === null || state.commitCount === undefined)
      ? "its commits"
      : countPhrase(state.commitCount, "commit");
    var line = C.h("div", { className: "spawn-status-line success" });
    line.appendChild(document.createTextNode(
      "Discarded " + (state.branch || "the branch") + " -- " + commits + " and " +
      countPhrase((state.discardedPaths || []).length, "uncommitted file") + " gone." +
      (state.recoveryTag
        ? " Tagged " + state.recoveryTag + ", so the commits survive a git gc. Recover with:"
        : "")));
    if (state.recoveryCommand) {
      var row = C.h("div", { className: "attach-row" });
      row.appendChild(C.h("code", { text: state.recoveryCommand }));
      var copyBtn = C.h("button", { className: "btn btn-sm", text: "Copy" });
      copyBtn.addEventListener("click", function () { C.copyText(state.recoveryCommand, copyBtn); });
      row.appendChild(copyBtn);
      line.appendChild(row);
    }
    return stopClick(line);
  }

  function renderAbandonStatusLine(state) {
    if (state.status === "error") {
      return stopClick(C.h("div", {
        className: "spawn-status-line error",
        text: "Could not abandon the worktree: " + state.error
      }));
    }
    var commits = (state.commitCount === null || state.commitCount === undefined)
      ? "Its commits stay"
      : "Its " + countPhrase(state.commitCount, "commit") + " stay";
    return stopClick(C.h("div", {
      className: "spawn-status-line success",
      text: "Removed the worktree for " + (state.branch || "the branch") + " (" +
        countPhrase((state.discardedPaths || []).length, "uncommitted file") +
        " discarded). " + commits + " on the parked branch, still mergeable."
    }));
  }

  // ------------------------------------------------------------------
  // End session (task-38)
  // ------------------------------------------------------------------
  //
  // POST /api/end-session kills exactly the one live tmux session for
  // (projectName, taskId) -- see server.py's _handle_end_session, which
  // resolves the same exact session name spawn() itself uses and kills
  // it with a leading "=" (exact-name match only). No confirm-arm here
  // (unlike Re-spawn/Resume, which start something new): this only ever
  // targets a session already finished/likely-finished/waiting, and the
  // worktree/branch are untouched either way -- Merge is still there
  // afterward regardless of whether this was clicked a moment too soon.

  // `openDrawerOnSuccess` (task-46): true only for the sessions panel's
  // own End-session button (see renderSessionsPanel) -- ending a
  // session there makes the row vanish with no path back to Merge, so
  // that variant opens the task's drawer on success, the single
  // lifecycle surface, so the merge-eligible state (or gate feedback)
  // is immediately visible. The drawer's own End-session button (see
  // renderDrawerSessionArea) omits the flag -- it's already looking at
  // the drawer, nothing more to open.
  function endSession(projectName, taskId, openDrawerOnSuccess) {
    var key = C.spawnKey(projectName, taskId);
    C.endSessionStates[key] = { status: "loading" };
    C.renderSessionsPanel();
    C.renderBoard();
    if (C.currentDrawer && C.currentDrawer.project === projectName && C.currentDrawer.id === taskId) {
      C.renderDrawerSessionArea();
    }

    fetch("/api/end-session", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ project: projectName, taskId: taskId })
    }).then(function (res) {
      return res.json().catch(function () { return {}; }).then(function (data) {
        if (!res.ok) throw new Error((data && data.error) || ("HTTP " + res.status));
        return data;
      });
    }).then(function () {
      C.endSessionStates[key] = { status: "success" };
      // task-94: the session did work after the last Merge click, so any
      // blocked/error gate verdict for this task predates that work --
      // drop it before the renders below, which are exactly the renders
      // that lift the live-session suppression and would otherwise show
      // it again.
      invalidateStaleMergeVerdict(projectName, taskId);
      if (openDrawerOnSuccess) {
        // Falls back gracefully (no drawer, no error) if the task isn't
        // in the currently loaded board data -- same reasoning as a
        // session row that never became clickable in the first place.
        var task = C.findTask(projectName, taskId);
        if (task) C.openDrawer({ name: projectName }, task);
      }
      // The session is gone -- everything downstream of it (sessions
      // list, board's live-session gating on the Merge button) needs a
      // real refetch, same as a successful merge.
      C.doRefresh(true);
    }).catch(function (err) {
      C.endSessionStates[key] = { status: "error", error: err.message || String(err) };
    }).then(function () {
      C.renderSessionsPanel();
      C.renderBoard();
      if (C.currentDrawer && C.currentDrawer.project === projectName && C.currentDrawer.id === taskId) {
        C.renderDrawerSessionArea();
      }
    });
  }

  // task-132: what End session re-renders when its arm changes. The
  // board has no End session button, so unlike refreshHarvestButtons
  // this touches only the two surfaces that draw one: the sidebar's
  // session rows and the drawer's session area, when the drawer is on
  // this task. Both read the same pending entry, so arming in one
  // place shows armed in the other.
  function refreshEndSessionButtons(projectName, taskId) {
    C.renderSessionsPanel();
    if (C.currentDrawer && C.currentDrawer.project === projectName && C.currentDrawer.id === taskId) {
      C.renderDrawerSessionArea();
    }
  }

  // task-132: `armReason` (from C.endSessionArmReason) is the sentence
  // a state that should not be killed on ONE click carries -- today
  // only "working". Without one the click ends the session at once,
  // exactly as before. With one this is the two-click arm cleanup and
  // the throwaway actions use: the first click arms an entry in
  // endSessionConfirmPending and re-labels the button; a second click
  // inside C.SPAWN_CONFIRM_WINDOW_MS ends the session; the window
  // expiring disarms with no action and puts the resting label back.
  // task-120 rendered this state DISABLED, carrying the reason as a
  // tooltip, which took the button away in the one state a user
  // actually reaches for it -- a spawn by mistake, the wrong task, an
  // agent looping or stuck in a long tool call are all mid-turn.
  function handleEndSessionClick(projectName, taskId, openDrawerOnSuccess, armReason) {
    var key = C.spawnKey(projectName, taskId);
    if (!armReason || harvestActionIsArmed(C.endSessionConfirmPending, key)) {
      delete C.endSessionConfirmPending[key];
      endSession(projectName, taskId, openDrawerOnSuccess);
      return;
    }
    C.endSessionConfirmPending[key] = { expires: Date.now() + C.SPAWN_CONFIRM_WINDOW_MS };
    refreshEndSessionButtons(projectName, taskId);
    setTimeout(function () {
      var current = C.endSessionConfirmPending[key];
      if (current && current.expires <= Date.now()) {
        delete C.endSessionConfirmPending[key];
        refreshEndSessionButtons(projectName, taskId);
      }
    }, C.SPAWN_CONFIRM_WINDOW_MS + 100);
  }

  // The armed label says, in the control itself, why a second click is
  // being asked for: a sidebar row has no room for a line under the
  // button, and a tooltip is only read by someone who already suspects
  // there is one. The full sentence stays as the title.
  var END_SESSION_ARMED_LABEL = "Agent is mid-turn — end anyway?";
  var END_SESSION_TOOLTIP = "End this agent's tmux session -- the worktree and branch are untouched," +
    " and this unlocks Merge if it was waiting on a live session.";

  function endSessionButtonDisplay(state, armReason, armed) {
    if (state && state.status === "loading") return { text: "Ending…", disabled: true, confirming: false, title: END_SESSION_TOOLTIP };
    if (armReason && armed) return { text: END_SESSION_ARMED_LABEL, disabled: false, confirming: true, title: armReason };
    return { text: "End session", disabled: false, confirming: false, title: END_SESSION_TOOLTIP };
  }

  function endSessionIsArmed(projectName, taskId) {
    return harvestActionIsArmed(C.endSessionConfirmPending, C.spawnKey(projectName, taskId));
  }

  // The one disabled rendering left is the in-flight "Ending…": the
  // button is never withheld for an agent state (task-132), so it
  // always carries its handler and always reaches handleEndSessionClick.
  function renderEndSessionButton(projectName, taskId, className, openDrawerOnSuccess, armReason) {
    var key = C.spawnKey(projectName, taskId);
    var display = endSessionButtonDisplay(C.endSessionStates[key], armReason,
                                          endSessionIsArmed(projectName, taskId));
    var btn = C.h("button", {
      className: className + (display.confirming ? " confirming" : ""),
      text: display.text
    });
    btn.disabled = display.disabled;
    btn.title = display.title;
    btn.addEventListener("click", function (e) {
      e.stopPropagation();
      handleEndSessionClick(projectName, taskId, openDrawerOnSuccess, armReason);
    });
    return btn;
  }

  function renderEndSessionStatusLine(state) {
    if (!state || state.status !== "error") return null;
    var line = C.h("div", { className: "spawn-status-line error", text: "Failed to end session: " + state.error });
    line.addEventListener("click", function (e) { e.stopPropagation(); });
    return line;
  }

  // ------------------------------------------------------------------
  // Seam: what the other files reach for through window.Centrale.
  // ------------------------------------------------------------------

  C.abandonButtonDisplay = abandonButtonDisplay;
  C.adoptDoneButtonDisplay = adoptDoneButtonDisplay;
  C.cleanupButtonDisplay = cleanupButtonDisplay;
  C.discardButtonDisplay = discardButtonDisplay;
  C.discardMainButtonDisplay = discardMainButtonDisplay;
  C.effectiveHasSpawnBranch = effectiveHasSpawnBranch;
  C.endSessionIsArmed = endSessionIsArmed;
  C.handleAbandonWorktreeClick = handleAbandonWorktreeClick;
  C.handleAdoptDoneClick = handleAdoptDoneClick;
  C.handleCleanupClick = handleCleanupClick;
  C.handleDiscardAttemptClick = handleDiscardAttemptClick;
  C.handleDiscardMainClick = handleDiscardMainClick;
  C.handleEndSessionClick = handleEndSessionClick;
  C.handleReconcileClick = handleReconcileClick;
  C.harvestAllReady = harvestAllReady;
  C.harvestButtonDisplay = harvestButtonDisplay;
  C.harvestTask = harvestTask;
  C.invalidateStaleMergeVerdict = invalidateStaleMergeVerdict;
  C.reconcileButtonDisplay = reconcileButtonDisplay;
  C.renderAbandonStatusLine = renderAbandonStatusLine;
  C.renderCleanupStatusLine = renderCleanupStatusLine;
  C.renderDiscardStatusLine = renderDiscardStatusLine;
  C.renderEndSessionButton = renderEndSessionButton;
  C.renderEndSessionStatusLine = renderEndSessionStatusLine;
  C.renderHarvestStatusLine = renderHarvestStatusLine;
})(window.Centrale = window.Centrale || {});
