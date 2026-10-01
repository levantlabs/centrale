// static/spawn.js -- the Spawn/Respawn/Resume buttons and their POSTs
//
// One of the per-concern frontend files loaded in order by
// static/index.html (task-89); the seam between them is the single
// window.Centrale namespace object, taken here as C. See
// static/state.js for the two rules that seam follows.
(function (C) {
  "use strict";

  // ------------------------------------------------------------------
  // Spawn logic
  // ------------------------------------------------------------------

  function spawnKey(projectName, taskId) {
    return projectName + "::" + taskId;
  }

  function refreshSpawnButtons(projectName, taskId) {
    C.renderBoard();
    if (C.currentDrawer && C.currentDrawer.project === projectName && C.currentDrawer.id === taskId) {
      C.renderDrawerSpawnArea();
      C.renderDrawerHarvestArea();
      C.renderDrawerSessionArea();
    }
  }

  // task-171: which agent a Spawn click would launch, resolved the way
  // the server does (spawn.choose_agent) from GET /api/board's
  // spawnAgents: a picked agent, else the first assignee when it names a
  // configured agent, else -- only with requireAgentAssignment off -- the
  // default. `needsPick` is true whenever the assignee names no agent, so
  // the picker stays on screen (and changeable) after a pick. A board
  // without spawnAgents (an older server) keeps the old assignee label
  // and offers no picker.
  function spawnAgentPlan(task, projectName, taskId) {
    var summary = C.boardData && C.boardData.spawnAgents;
    var assignee = task ? C.firstAssignee(task) : null;
    if (!summary || !Array.isArray(summary.names)) {
      return { agent: assignee ? assignee.toLowerCase() : null, source: assignee ? "assignee" : null,
               needsPick: false, names: [], required: false, defaultAgent: null };
    }
    var names = summary.names.map(function (n) { return String(n); });
    var lower = names.map(function (n) { return n.toLowerCase(); });
    var plan = { agent: null, source: null, needsPick: false, names: names,
                 required: summary.required !== false, defaultAgent: summary.defaultAgent || null };
    var choice = C.spawnAgentChoices[spawnKey(projectName, taskId)];
    var assigneeResolves = !!assignee && lower.indexOf(assignee.toLowerCase()) !== -1;
    plan.needsPick = !assigneeResolves;
    if (choice && lower.indexOf(String(choice).toLowerCase()) !== -1) {
      plan.agent = String(choice).toLowerCase();
      plan.source = "choice";
    } else if (assigneeResolves) {
      plan.agent = assignee.toLowerCase();
      plan.source = "assignee";
    } else if (!plan.required && plan.defaultAgent) {
      plan.agent = String(plan.defaultAgent).toLowerCase();
      plan.source = "default";
    }
    return plan;
  }

  // Why the spawn control looks the way it does, for its tooltip: the
  // assignee it could not resolve, and what happens about it.
  function spawnAgentReason(task, plan) {
    if (!plan.needsPick) return "";
    var assignee = task ? C.firstAssignee(task) : null;
    var why = assignee
      ? ("@" + assignee + " is not a configured agent")
      : "This task has no assignee";
    if (plan.source === "choice") return why + " -- " + plan.agent + " will run, and be recorded as its assignee.";
    if (plan.source === "default") {
      return why + " -- the default agent (" + plan.agent + ") will run, and be recorded as its assignee. Pick another to override.";
    }
    return why + " -- pick the agent to run. It will be recorded as the task's assignee.";
  }

  // The picker offered beside the spawn button when the task's assignee
  // names no configured agent. Its value lives in C.spawnAgentChoices,
  // not in the element, so a board re-render keeps the pick.
  function renderSpawnAgentPicker(task, projectName, taskId, plan) {
    var key = spawnKey(projectName, taskId);
    var select = C.h("select", {
      className: "spawn-agent-picker",
      title: spawnAgentReason(task, plan),
      attrs: { "aria-label": "Agent to spawn" }
    });
    var placeholder = document.createElement("option");
    placeholder.value = "";
    placeholder.textContent = plan.required
      ? "Choose agent…"
      : ("Default (" + plan.defaultAgent + ")");
    select.appendChild(placeholder);
    plan.names.forEach(function (name) {
      var opt = document.createElement("option");
      opt.value = name;
      opt.textContent = name;
      select.appendChild(opt);
    });
    var choice = C.spawnAgentChoices[key];
    select.value = plan.source === "choice" && choice ? choice : "";
    // Picking must neither open the drawer (the card's own click) nor
    // spawn anything: it only changes what the button will launch.
    select.addEventListener("click", function (e) { e.stopPropagation(); });
    select.addEventListener("change", function (e) {
      if (e && e.stopPropagation) e.stopPropagation();
      if (select.value) C.spawnAgentChoices[key] = select.value;
      else delete C.spawnAgentChoices[key];
      delete C.spawnConfirmPending[key];
      refreshSpawnButtons(projectName, taskId);
    });
    return select;
  }

  // The three confirm-armed actions -- Spawn, Re-spawn and Resume --
  // share one pending entry per task (spawnConfirmPending), so the entry
  // has to say WHICH of them is armed. Before task-116 that was implicit:
  // the three were mutually exclusive on screen, so whatever was armed
  // was the only thing that could have armed it. Now Resume and Re-spawn
  // render together (see renderDrawerSpawnArea), and without this an
  // armed Resume would light up Re-spawn's button too -- and confirming
  // there would start a fresh prompt instead of continuing the
  // conversation. A pending armed by another action is not this button's:
  // clicking simply re-arms for the one that was clicked.
  function armedFor(pending, action) {
    return !!(pending && pending.expires > Date.now() && pending.action === action);
  }

  // Computes what a spawn button should show/do for (projectName, taskId).
  // `live` is the existing-session lookup the caller already has (kept as
  // a param rather than recomputed, since callers already need it for
  // other purposes too). This never touches the disabled/"Session live"
  // path -- the confirm step only ever applies when the button would
  // otherwise be a plain, enabled "Spawn" click.
  function spawnButtonDisplay(task, projectName, taskId, live, state) {
    var plan = spawnAgentPlan(task, projectName, taskId);
    if (!C.isTmuxAvailable()) {
      return { text: C.spawnButtonLabel(plan), disabled: true, confirming: false, noTmux: true, plan: plan };
    }
    if (live) return { text: "Session live", disabled: true, confirming: false };
    if (state && state.status === "loading") return { text: "Spawning…", disabled: true, confirming: false };

    var key = spawnKey(projectName, taskId);
    var pending = C.spawnConfirmPending[key];
    if (armedFor(pending, "spawn")) {
      var count = pending.count;
      var sessionsText = count > 0 ? (count + " agent" + (count === 1 ? "" : "s") + " active") : null;
      if (pending.claimMessage) {
        // The out-of-board claim (task-40) is the priority reason to
        // show -- an unrelated live session elsewhere in the project (if
        // any) folds into the same title rather than chaining a second
        // confirm (see handleSpawnClick).
        return {
          // task-134: the short label follows the message -- "Claimed
          // elsewhere" for a real out-of-board claim, "Discarded" when
          // a recovery tag says the user threw the last attempt away.
          text: pending.claimLabel || "Claimed elsewhere — spawn anyway?",
          longText: pending.claimMessage,
          confirmTitle: sessionsText
            ? (pending.claimMessage + "\n\n" + sessionsText + " elsewhere in this project.")
            : pending.claimMessage,
          disabled: false,
          confirming: true
        };
      }
      return {
        text: sessionsText + " — spawn anyway?",
        confirmTitle: C.findOtherLiveSessionsInProject(projectName, taskId).map(C.describeSessionFiles).join("\n"),
        disabled: false,
        confirming: true
      };
    }
    // task-171: with nothing picked and no agent the server would
    // accept, the button says so and waits for the picker.
    return {
      text: C.spawnButtonLabel(plan),
      disabled: !plan.agent,
      confirming: false,
      agentTitle: spawnAgentReason(task, plan),
      plan: plan
    };
  }

  // A task can already be claimed by a worker the board can't see -- a
  // self-directed agent working through the backlog on its own, a
  // teammate, a delegated subagent -- visible only as status "In
  // Progress" (task-40). Spawning onto it without asking risks starting
  // a second agent on work already underway elsewhere. Deliberately
  // reads the same `task` object callers already have (the board
  // summary's own status/assignees/updatedAt) rather than fetching
  // anything new -- this stays frontend-only, no new API calls.
  function isOutOfBoardClaim(task, projectName, taskId) {
    if (!task || task.status !== "In Progress") return false;
    if (C.findLiveSession(projectName, taskId)) return false;
    return !C.effectiveHasSpawnBranch(task, projectName, taskId);
  }

  // Click handler shared by the card's inline spawn button and the
  // drawer's spawn button. With no other live session in the same
  // project and no out-of-board claim, spawns immediately (unchanged
  // one-click behavior). Otherwise the first click arms a few-second
  // confirm window (the button flips to an informed "spawn anyway?"
  // prompt -- see spawnButtonDisplay); a second click inside that window
  // proceeds. The window auto-reverts if it lapses. When both an
  // overlapping session and an out-of-board claim apply, this arms a
  // single confirm carrying both reasons rather than chaining two.
  function handleSpawnClick(projectName, taskId) {
    // Guards the disabled-button state above; a no-op if reached anyway
    // (e.g. a stray programmatic click), since the button should never
    // be enabled while tmux is unavailable.
    if (!C.isTmuxAvailable()) return;
    // task-171: nothing to launch until an agent is picked (the button is
    // disabled then; this covers a stray programmatic click).
    if (!spawnAgentPlan(C.findTask(projectName, taskId), projectName, taskId).agent) return;

    var key = spawnKey(projectName, taskId);
    var pending = C.spawnConfirmPending[key];
    if (armedFor(pending, "spawn")) {
      delete C.spawnConfirmPending[key];
      spawnTask(projectName, taskId);
      return;
    }

    var others = C.findOtherLiveSessionsInProject(projectName, taskId);
    var task = C.findTask(projectName, taskId);
    var claimed = isOutOfBoardClaim(task, projectName, taskId);
    if (others.length === 0 && !claimed) {
      spawnTask(projectName, taskId);
      return;
    }

    C.spawnConfirmPending[key] = {
      expires: Date.now() + C.SPAWN_CONFIRM_WINDOW_MS,
      action: "spawn",
      count: others.length,
      claimMessage: claimed ? C.claimedElsewhereMessage(task) : null,
      claimLabel: claimed ? C.claimedElsewhereLabel(task) : null
    };
    refreshSpawnButtons(projectName, taskId);
    setTimeout(function () {
      var current = C.spawnConfirmPending[key];
      if (current && current.expires <= Date.now()) {
        delete C.spawnConfirmPending[key];
        refreshSpawnButtons(projectName, taskId);
      }
    }, C.SPAWN_CONFIRM_WINDOW_MS + 100);
  }

  // Drawer-only: sends an agent back into a task's EXISTING worktree/
  // branch (to fix a failed merge gate, or continue unfinished work) --
  // as opposed to handleSpawnClick, which starts fresh work on a task
  // that has none yet. Reuses the exact same armed-confirm mechanism
  // (spawnConfirmPending / SPAWN_CONFIRM_WINDOW_MS), but unlike a plain
  // spawn -- which only arms when another session is already live
  // elsewhere in the project -- this always requires the confirm click,
  // since re-spawning onto work that's otherwise ready to merge is
  // deliberate every time, not just when something else is in the way.
  function handleRespawnClick(projectName, taskId) {
    if (!C.isTmuxAvailable()) return;

    var key = spawnKey(projectName, taskId);
    var pending = C.spawnConfirmPending[key];
    if (armedFor(pending, "respawn")) {
      delete C.spawnConfirmPending[key];
      spawnTask(projectName, taskId);
      return;
    }

    C.spawnConfirmPending[key] = { expires: Date.now() + C.SPAWN_CONFIRM_WINDOW_MS, action: "respawn", count: 0 };
    refreshSpawnButtons(projectName, taskId);
    setTimeout(function () {
      var current = C.spawnConfirmPending[key];
      if (current && current.expires <= Date.now()) {
        delete C.spawnConfirmPending[key];
        refreshSpawnButtons(projectName, taskId);
      }
    }, C.SPAWN_CONFIRM_WINDOW_MS + 100);
  }

  // Drawer-only, for the more specific "interrupted" state (a branch with
  // no live session that either has uncommitted changes or whose own copy
  // of the task is still in an active status -- an agent's session died
  // mid-work, before or after it committed; see renderDrawerSpawnArea):
  // continues that same dead conversation via POST /api/resume, rather
  // than starting a fresh prompt like Re-spawn does. Same armed-confirm
  // mechanism, same "always requires the confirm click" reasoning as
  // handleRespawnClick -- and its own `action`, since the two buttons now
  // render together (see armedFor).
  function handleResumeClick(projectName, taskId) {
    if (!C.isTmuxAvailable()) return;

    var key = spawnKey(projectName, taskId);
    var pending = C.spawnConfirmPending[key];
    if (armedFor(pending, "resume")) {
      delete C.spawnConfirmPending[key];
      resumeTask(projectName, taskId);
      return;
    }

    C.spawnConfirmPending[key] = { expires: Date.now() + C.SPAWN_CONFIRM_WINDOW_MS, action: "resume", count: 0 };
    refreshSpawnButtons(projectName, taskId);
    setTimeout(function () {
      var current = C.spawnConfirmPending[key];
      if (current && current.expires <= Date.now()) {
        delete C.spawnConfirmPending[key];
        refreshSpawnButtons(projectName, taskId);
      }
    }, C.SPAWN_CONFIRM_WINDOW_MS + 100);
  }

  function spawnTask(projectName, taskId) {
    var key = spawnKey(projectName, taskId);
    C.spawnStates[key] = { status: "loading" };
    C.renderBoard();
    if (C.currentDrawer && C.currentDrawer.project === projectName && C.currentDrawer.id === taskId) {
      C.renderDrawerSpawnArea();
      C.renderDrawerHarvestArea();
      C.renderDrawerSessionArea();
    }

    // task-171: a picked agent is sent as the explicit choice; otherwise
    // the server resolves the assignee itself (and refuses to guess).
    var payload = { project: projectName, taskId: taskId };
    if (C.spawnAgentChoices[key]) payload.agent = C.spawnAgentChoices[key];
    fetch("/api/spawn", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload)
    }).then(function (res) {
      return res.json().catch(function () { return {}; }).then(function (data) {
        if (!res.ok) {
          throw new Error((data && data.error) || ("HTTP " + res.status));
        }
        return data;
      });
    }).then(function (data) {
      C.spawnStates[key] = { status: "success", session: data.session, attach: data.attach, agent: data.agent };
      // task-171: the claim recorded the pick as the assignee, so the
      // next board read resolves it without one.
      delete C.spawnAgentChoices[key];
      // task-94: an agent is about to work on this branch, so a
      // remembered blocked/error merge verdict for it is already stale
      // -- drop it (a successful merge/cleanup verdict is kept; see
      // invalidateStaleMergeVerdict). Client-side only, no refetch.
      C.invalidateStaleMergeVerdict(projectName, taskId);
      if (Array.isArray(data.warnings)) {
        data.warnings.forEach(function (w) { C.showToast(w, "error"); });
      }
      C.fetchSessions();
    }).catch(function (err) {
      C.spawnStates[key] = { status: "error", error: err.message || String(err) };
    }).then(function () {
      C.renderBoard();
      if (C.currentDrawer && C.currentDrawer.project === projectName && C.currentDrawer.id === taskId) {
        C.renderDrawerSpawnArea();
        C.renderDrawerHarvestArea();
        C.renderDrawerSessionArea();
      }
    });
  }

  // Same shape as spawnTask, posting to /api/resume instead -- shares
  // spawnStates (a resume in flight looks and behaves like a spawn in
  // flight to every other part of the UI that reads it).
  // task-66: reconcile=true is the SAME request with one extra flag
  // ({reconcile: true}) -- the server runs the same resume path with the
  // reconcile prompt instead; nothing else here differs.
  function resumeTask(projectName, taskId, reconcile) {
    var key = spawnKey(projectName, taskId);
    C.spawnStates[key] = { status: "loading" };
    C.renderBoard();
    if (C.currentDrawer && C.currentDrawer.project === projectName && C.currentDrawer.id === taskId) {
      C.renderDrawerSpawnArea();
      C.renderDrawerHarvestArea();
      C.renderDrawerSessionArea();
    }

    var payload = { project: projectName, taskId: taskId };
    if (reconcile) payload.reconcile = true;
    fetch("/api/resume", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload)
    }).then(function (res) {
      return res.json().catch(function () { return {}; }).then(function (data) {
        if (!res.ok) {
          throw new Error((data && data.error) || ("HTTP " + res.status));
        }
        return data;
      });
    }).then(function (data) {
      C.spawnStates[key] = { status: "success", session: data.session, attach: data.attach, agent: data.agent };
      // task-94: same as spawnTask -- and this is the path the reported
      // flow takes, since "Resume to reconcile" exists precisely to fix
      // the branch the blocked verdict was complaining about.
      C.invalidateStaleMergeVerdict(projectName, taskId);
      if (Array.isArray(data.warnings)) {
        data.warnings.forEach(function (w) { C.showToast(w, "error"); });
      }
      C.fetchSessions();
    }).catch(function (err) {
      C.spawnStates[key] = { status: "error", error: err.message || String(err) };
    }).then(function () {
      C.renderBoard();
      if (C.currentDrawer && C.currentDrawer.project === projectName && C.currentDrawer.id === taskId) {
        C.renderDrawerSpawnArea();
        C.renderDrawerHarvestArea();
        C.renderDrawerSessionArea();
      }
    });
  }

  // ------------------------------------------------------------------
  // Seam: what the other files reach for through window.Centrale.
  // ------------------------------------------------------------------

  C.armedFor = armedFor;
  C.handleRespawnClick = handleRespawnClick;
  C.handleResumeClick = handleResumeClick;
  C.handleSpawnClick = handleSpawnClick;
  C.resumeTask = resumeTask;
  C.renderSpawnAgentPicker = renderSpawnAgentPicker;
  C.spawnAgentPlan = spawnAgentPlan;
  C.spawnButtonDisplay = spawnButtonDisplay;
  C.spawnKey = spawnKey;
})(window.Centrale = window.Centrale || {});
