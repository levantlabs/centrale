// static/tasks.js -- pure task/session helpers: sorting, columns, filtering, milestones, session lookup, agent badges
//
// One of the per-concern frontend files loaded in order by
// static/index.html (task-89); the seam between them is the single
// window.Centrale namespace object, taken here as C. See
// static/state.js for the two rules that seam follows.
(function (C) {
  "use strict";

  // ------------------------------------------------------------------
  // Priority / sorting / column helpers
  // ------------------------------------------------------------------

  function firstAssignee(task) {
    var assignees = task.assignees || [];
    if (assignees.length === 0) return null;
    return String(assignees[0]).replace(/^@/, "").trim();
  }

  function assigneeLabel(task) {
    var name = firstAssignee(task);
    return name ? ("@" + name) : "unassigned";
  }

  function spawnButtonLabel(task) {
    var name = firstAssignee(task);
    return name ? ("Spawn (" + name.toLowerCase() + ")") : "Spawn agent";
  }

  // Coarse relative time for the out-of-board-claim confirm message
  // (task-40) -- minutes/hours/days, matching the granularity a human
  // skimming "claimed by @x, updated N ago" actually needs. Returns null
  // for a missing/unparseable timestamp so callers can drop the clause
  // entirely rather than print "updated null ago".
  function relTime(iso) {
    if (!iso) return null;
    var then = new Date(iso).getTime();
    if (isNaN(then)) return null;
    var diffMs = Math.max(0, Date.now() - then);
    var mins = Math.floor(diffMs / 60000);
    if (mins < 1) return "just now";
    if (mins < 60) return mins + " minute" + (mins === 1 ? "" : "s") + " ago";
    var hours = Math.floor(mins / 60);
    if (hours < 24) return hours + " hour" + (hours === 1 ? "" : "s") + " ago";
    var days = Math.floor(hours / 24);
    return days + " day" + (days === 1 ? "" : "s") + " ago";
  }

  // task-70: the board's branchCheckout for a branch checked out in a
  // worktree Centrale doesn't manage (kind "external"), or null for a
  // task with no branch, a Centrale-managed checkout, or a parked branch
  // (kind "none" -- checked out nowhere, rendered differently on purpose).
  function externalCheckout(task) {
    var c = task && task.branchCheckout;
    return (c && c.kind === "external") ? c : null;
  }

  function parkedBranch(task) {
    var c = task && task.branchCheckout;
    return !!(c && c.kind === "none");
  }

  // "worked externally at <path> -- last commit N ago": the path is the
  // foreign checkout git reports, the age is the branch tip's commit
  // time -- the only freshness signal Centrale has for work it can't
  // see (no session, no hooks, no worktree of its own).
  function externalCheckoutSummary(ext) {
    var age = relTime(ext.lastCommitAt);
    return "Worked externally at " + ext.path + (age ? " -- last commit " + age : "");
  }

  function externalCheckoutReason(ext) {
    return externalCheckoutSummary(ext)
      + ". Spawn, Resume and clean-up are disabled: git refuses a second checkout of the same branch"
      + " and won't delete a branch that is checked out."
      + " Finish or remove that worktree to hand the branch back to Centrale.";
  }

  // task-80: the one disabled "Worked externally" button every action
  // that cannot succeed on an externally checked-out branch renders in
  // place of its clickable self (the card footer's and the drawer's
  // "Merged -- clean up"; the drawer's Resume/Re-spawn go through
  // renderDrawerExternalWork, which adds the reason line too). Carries
  // the same honest sentence POST /api/cleanup-branch, /api/spawn and
  // /api/resume refuse with, as its tooltip. Never clickable: no
  // handler, so nothing can reach the server's 409 from here.
  function externalWorkButton(ext, className) {
    var btn = C.h("button", { className: className, text: "Worked externally" });
    btn.disabled = true;
    btn.title = externalCheckoutReason(ext);
    return btn;
  }

  // task-81: the reason a Merge offered on an external checkout is shown
  // disabled -- the same sentence family as gate 2's failure ("checked
  // out outside Centrale at <path> ... merge once that worktree is
  // finished and removed"), so the card says up front what a click
  // could only refuse with, without the harvest round trip or the
  // blocked events-log entry it would cost.
  function externalMergeReason(ext) {
    return externalCheckoutSummary(ext)
      + ". Merge is disabled while the branch is checked out outside Centrale:"
      + " merge becomes possible once that worktree is finished and removed.";
  }

  var MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

  function pad2(n) { return (n < 10 ? "0" : "") + n; }

  // task-134: the wall-clock time of a discard, as the person who did
  // it would say it -- "17:05 today", "17:05 yesterday", "17:05 on
  // 2 Sep". Not relTime()'s "24 minutes ago": a discard is a single
  // remembered act, and the user is being asked whether THAT is the
  // thing the board is still showing, so naming the clock they did it
  // by identifies it better than an elapsed count. Local time, because
  // the board's lastDiscardedAt is UTC and 17:05 is what they saw.
  // Returns null for missing/unparseable input, so callers can fall
  // back to the copy that doesn't mention a discard at all.
  function discardClock(iso) {
    if (!iso) return null;
    var t = new Date(iso);
    if (isNaN(t.getTime())) return null;
    var clock = pad2(t.getHours()) + ":" + pad2(t.getMinutes());
    var now = new Date(Date.now());
    // Midnight is taken from the calendar rather than by subtracting
    // 86400000, so a DST change doesn't shift "yesterday" by an hour.
    var startOfToday = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
    var startOfYesterday = new Date(now.getFullYear(), now.getMonth(), now.getDate() - 1).getTime();
    if (t.getTime() >= startOfToday) return clock + " today";
    if (t.getTime() >= startOfYesterday) return clock + " yesterday";
    return clock + " on " + t.getDate() + " " + MONTHS[t.getMonth()];
  }

  // The informed confirm message for spawning onto a task already
  // claimed outside the board (task-40): built entirely from data the
  // board summary already has (assignees, updatedAt) -- no new API call.
  // Degrades gracefully when either piece is missing (AC #3).
  //
  // task-134: with one exception, and it is the common one. The guard
  // that reaches here has already established there is no branch, no
  // worktree and no session -- and a task in that state carrying a
  // recovery tag was not claimed by anyone: the user discarded the last
  // attempt themselves, at the time the tag names (lastDiscardedAt).
  // Telling them "claimed elsewhere, updated 24 min ago" about work
  // they threw away seconds earlier describes a situation they know is
  // not the case. The confirm still arms -- the task IS still In
  // Progress with nobody on it, and one more click before spawning onto
  // it is right -- only the sentence changes.
  function claimedElsewhereMessage(task) {
    var discarded = discardClock(task && task.lastDiscardedAt);
    if (discarded) {
      return "Previous attempt discarded " + discarded + "; still "
        + (task.status || "In Progress") + " on main. Spawn fresh?";
    }
    var name = firstAssignee(task);
    var rel = relTime(task.updatedAt);
    var clauses = [];
    if (name) clauses.push("claimed by @" + name);
    if (rel) clauses.push("updated " + rel);
    return clauses.length
      ? ("In Progress — " + clauses.join(", ") + ". Spawn anyway?")
      : "In Progress. Spawn anyway?";
  }

  // The short form of the same thing: the card's inline button has room
  // for a label, not a sentence (the sentence is its tooltip, and the
  // drawer's button label). Kept beside the message it shortens so the
  // two can never disagree about which case they are describing.
  function claimedElsewhereLabel(task) {
    return discardClock(task && task.lastDiscardedAt)
      ? "Discarded — spawn fresh?"
      : "Claimed elsewhere — spawn anyway?";
  }

  var TMUX_UNAVAILABLE_TOOLTIP = "Spawning requires tmux (e.g. sudo apt install tmux)";

  // Reads live from boardData rather than a separately tracked flag, so
  // it can never drift out of sync with the last /api/board response.
  // Defaults to "available" until a response says otherwise, matching
  // the server's own fail-open default.
  function isTmuxAvailable() {
    return !(C.boardData && C.boardData.capabilities && C.boardData.capabilities.tmux === false);
  }

  function priorityRank(p) {
    if (p === "high") return 0;
    if (p === "medium") return 1;
    if (p === "low") return 2;
    return 3;
  }

  function compareTasks(a, b) {
    var pr = priorityRank(a.task.priority) - priorityRank(b.task.priority);
    if (pr !== 0) return pr;
    var oa = typeof a.task.ordinal === "number" ? a.task.ordinal : 0;
    var ob = typeof b.task.ordinal === "number" ? b.task.ordinal : 0;
    return oa - ob;
  }

  function computeColumns(projects) {
    var seen = {};
    var extras = [];
    projects.forEach(function (p) {
      (p.statuses || []).forEach(function (s) {
        if (!seen[s]) {
          seen[s] = true;
          if (C.CANONICAL_STATUSES.indexOf(s) === -1) extras.push(s);
        }
      });
    });
    var columns = C.CANONICAL_STATUSES.filter(function (c) { return seen[c]; });
    return columns.concat(extras);
  }

  function isFirstColumnStatus(project, status) {
    var statuses = project.statuses || [];
    return statuses.length > 0 && statuses[0] === status;
  }

  // task-116: "someone is (or was) working on this" expressed without
  // naming a single status, since statuses are configured per repo:
  // anything that is neither the terminal Done nor the board's first,
  // not-yet-started column. An empty/missing status is not active --
  // it says nothing was read, not that work is under way. Built from
  // the two helpers the rest of the frontend already decides with, so a
  // repo with custom columns gets the same answer everywhere.
  function isActiveStatus(project, status) {
    if (!String(status || "").trim()) return false;
    if (C.isDoneStatus(status)) return false;
    return !isFirstColumnStatus(project, status);
  }

  // ------------------------------------------------------------------
  // Filtering
  // ------------------------------------------------------------------

  function taskMatchesSearch(task, search) {
    if (!search) return true;
    var values = [task.title, task.id].concat(task.labels || []);
    return values.some(function (value) {
      return String(value || "").toLowerCase().indexOf(search) !== -1;
    });
  }

  function getFilteredItems() {
    var items = [];
    var search = C.searchText.trim().toLowerCase();
    (C.boardData.projects || []).forEach(function (project) {
      if (C.activeProjects && !C.activeProjects.has(project.name)) return;
      (project.tasks || []).forEach(function (task) {
        if (C.readyOnly && !task.ready) return;
        // task-86: one more gate on the same path readyOnly and search
        // already take, so every lane in every project narrows together
        // -- there is no per-column milestone logic. task-91: the
        // comparison is on the project-qualified key, not the bare id,
        // because "m-0" means a different milestone in every repo.
        if (C.milestoneFilter && milestoneKey(project.name, taskMilestone(task)) !== C.milestoneFilter) return;
        if (!taskMatchesSearch(task, search)) return;
        items.push({ task: task, project: project });
      });
    });
    return items;
  }

  // ------------------------------------------------------------------
  // Milestones (task-86, corrected by task-91)
  //
  // Centrale is a thin veneer here: it only SURFACES the milestone
  // backlog already puts on every task. Creating, renaming and closing
  // milestones stays the backlog CLI's job, so there is no milestone
  // endpoint and no milestone state of Centrale's own -- the dropdown is
  // derived from the loaded board on every render.
  //
  // Two things about backlog's milestones shape everything below:
  //   * a task carries its milestone's ID ("m-0"), never its title. The
  //     server resolves id -> title per project and hands it back as
  //     task.milestoneTitle (see _load_project_milestones); the id stays
  //     the identity, the title is display only, so a renamed milestone
  //     never invalidates a selection.
  //   * ids are assigned per repo and sequentially, so EVERY repo's
  //     first milestone is "m-0". Milestone identity across the board is
  //     therefore project + id -- never the id alone.
  // ------------------------------------------------------------------

  // "" for a task with no milestone (missing, null, or whitespace), so
  // callers never have to distinguish those three. This is the ID: what
  // the filter matches on, never what the user is shown.
  function taskMilestone(task) {
    return task && task.milestone ? String(task.milestone).trim() : "";
  }

  // The title the server resolved for this task's milestone, or "" if it
  // couldn't (a project with no milestone file, a `backlog milestone
  // list` this build can't parse) -- or if the task didn't come off the
  // board payload at all, which is the only place the titles ride.
  function ownMilestoneTitle(task) {
    return task && task.milestoneTitle ? String(task.milestoneTitle).trim() : "";
  }

  // What the user is shown for a BOARD task: the milestone's title
  // ("post-0.2.0"), falling back to its id, so an unresolvable title
  // degrades to the pre-task-91 display rather than to a blank chip.
  function taskMilestoneLabel(task) {
    var id = taskMilestone(task);
    return id ? (ownMilestoneTitle(task) || id) : "";
  }

  // The title a milestone id has in one project, looked up in the board
  // data already in memory. The drawer needs this: its task comes from
  // /api/task, a passthrough of `task view --json`, which carries the id
  // and no title -- resolved titles ride on the board payload, resolved
  // once per project there rather than once per drawer open.
  function milestoneTitleIn(projectName, id) {
    var found = "";
    (C.boardData.projects || []).forEach(function (project) {
      if (found || project.name !== projectName) return;
      (project.tasks || []).forEach(function (task) {
        if (found || taskMilestone(task) !== id) return;
        found = ownMilestoneTitle(task);
      });
    });
    return found;
  }

  // The one label both milestone chips render -- the card's, whose task
  // carries its own title, and the drawer's, whose task doesn't.
  function milestoneChipLabel(projectName, task) {
    var id = taskMilestone(task);
    if (!id) return "";
    return ownMilestoneTitle(task) || milestoneTitleIn(projectName, id) || id;
  }

  // task-91: one string carrying both halves of a milestone's identity,
  // so it can be an <option> value, the persisted selection, and a
  // comparison in the filter gate. Both halves are percent-encoded, which
  // is what keeps the "/" a separator and never a character out of a
  // project name or an id.
  function milestoneKey(projectName, id) {
    return encodeURIComponent(projectName || "") + "/" + encodeURIComponent(id || "");
  }

  // The inverse, or null for anything that isn't a key (a pre-task-91
  // bare id, a hand-edited localStorage value with broken escapes).
  function parseMilestoneKey(key) {
    var raw = String(key || "");
    var sep = raw.indexOf("/");
    if (sep < 0) return null;
    try {
      return {
        project: decodeURIComponent(raw.slice(0, sep)),
        id: decodeURIComponent(raw.slice(sep + 1))
      };
    } catch (e) {
      return null;
    }
  }

  // Every milestone present on the currently ACTIVE projects, one entry
  // per project + id, with a done/total count each (the existing
  // isDoneStatus normalization, same as the sidebar's per-project
  // counts). Scoped to the active projects deliberately: the project
  // chips choose which repos are on screen at all, so a milestone from a
  // hidden project has nothing to narrow. readyOnly/search are NOT
  // applied -- those refine what is already in view, and folding them in
  // would make the counts jump around as an unrelated filter changes.
  //
  // task-91: this used to fold a shared id into ONE entry spanning
  // projects, reasoning that it focused a release wave across the board.
  // With ids assigned per repo that fold was wrong by construction: it
  // blended this repo's finished "m-0" with another repo's untouched
  // "m-0" into a single entry filtering both repos as one milestone
  // that exists nowhere. Entries are per project now; the dropdown
  // grows with the number of milestones actually in use, which is what
  // it always described.
  function collectMilestones() {
    var byKey = Object.create(null);
    var entries = [];
    (C.boardData.projects || []).forEach(function (project) {
      if (C.activeProjects && !C.activeProjects.has(project.name)) return;
      (project.tasks || []).forEach(function (task) {
        var id = taskMilestone(task);
        if (!id) return;
        var key = milestoneKey(project.name, id);
        if (!byKey[key]) {
          byKey[key] = {
            key: key,
            id: id,
            project: project.name,
            title: taskMilestoneLabel(task),
            total: 0,
            done: 0
          };
          entries.push(byKey[key]);
        }
        byKey[key].total++;
        if (C.isDoneStatus(task.status)) byKey[key].done++;
      });
    });
    entries.sort(function (a, b) {
      return a.title.localeCompare(b.title) ||
        a.project.localeCompare(b.project) ||
        a.id.localeCompare(b.id);
    });
    return entries;
  }

  // "post-0.2.0 · 0/7 done", or "post-0.2.0 · my-lib · 0/7 done" when two
  // projects on the board have a milestone by that same title -- the
  // project name is spelled out only where it is the thing telling two
  // entries apart, so the common case stays short.
  function milestoneOptionLabel(m, qualify) {
    return m.title + " · " + (qualify ? m.project + " · " : "") + m.done + "/" + m.total + " done";
  }

  // Rebuilt only when the derived option list or the selection actually
  // changes: a <select> rebuilt under an open dropdown would close it,
  // and this runs on every renderAll (i.e. every changed refresh).
  function renderMilestoneFilter() {
    var field = C.byId("milestone-field");
    var select = C.byId("milestone-filter");
    var options = collectMilestones();
    // A stored/selected milestone that has vanished from the board (its
    // tasks completed into backlog/completed/, a project toggled off)
    // stays in the list showing 0/0 rather than resetting the selection
    // behind the user's back -- an empty board then has a visible reason.
    // Its title went with its tasks, so the entry falls back to the id.
    if (C.milestoneFilter && !options.some(function (m) { return m.key === C.milestoneFilter; })) {
      var gone = parseMilestoneKey(C.milestoneFilter);
      options = options.concat([{
        key: C.milestoneFilter,
        id: gone ? gone.id : C.milestoneFilter,
        project: gone ? gone.project : "",
        title: gone ? gone.id : C.milestoneFilter,
        total: 0,
        done: 0
      }]);
    }
    var signature = JSON.stringify([C.milestoneFilter, options]);
    if (signature === C.lastMilestoneFilterSignature) return;
    C.lastMilestoneFilterSignature = signature;

    // Which titles are ambiguous on THIS board -- computed over the
    // options actually about to be rendered, so the qualifier appears
    // and disappears with the project selection.
    var titleCounts = Object.create(null);
    options.forEach(function (m) {
      titleCounts[m.title] = (titleCounts[m.title] || 0) + 1;
    });

    C.clearChildren(select);
    select.appendChild(C.h("option", { attrs: { value: "" }, text: "All milestones" }));
    options.forEach(function (m) {
      select.appendChild(C.h("option", {
        attrs: { value: m.key },
        text: milestoneOptionLabel(m, titleCounts[m.title] > 1)
      }));
    });
    select.value = C.milestoneFilter;
    // Nothing to choose from on a board with no milestones at all: the
    // whole field stays out of the filter row rather than offering an
    // empty dropdown.
    field.hidden = options.length === 0;
  }

  // ------------------------------------------------------------------
  // Session parsing / spawn-live lookups
  // ------------------------------------------------------------------

  // task-59: a session name embeds a tmux-safe "_"-for-"." encoding of
  // a dotted subtask id (e.g. "TASK-11.2" -> "task-11_2") -- tmux
  // forbids "." in a session name (it's the window/pane-index separator
  // in a -t target) and silently rewrites it out from under a caller
  // who asks for one anyway, verified empirically -- so the remainder
  // is matched against the encoded shape and decoded back ("_" -> ".")
  // rather than matched against the dotted shape directly. Mirrors
  // server.py's spawn.encode_task_id_for_session/decode_session_task_id;
  // a real task id never contains "_" (only letters, digits, "-", "."),
  // so this decode is unambiguous, not a hopeful blind reversal.
  //
  // SESSION_PREFIX mirrors server.py's SESSION_PREFIX -- the one prefix
  // every session name carries.
  var SESSION_PREFIX = "centrale-";
  function parseSessionTask(sessionName) {
    if (sessionName.indexOf(SESSION_PREFIX) !== 0) return null;
    var rest = sessionName.slice(SESSION_PREFIX.length);
    var candidates = C.knownProjects.filter(function (p) {
      return rest === p || rest.indexOf(p + "-") === 0;
    });
    candidates.sort(function (a, b) { return b.length - a.length; });
    for (var i = 0; i < candidates.length; i++) {
      var proj = candidates[i];
      var remainder = rest.slice(proj.length + 1);
      var m = remainder.match(/^([A-Za-z]+)-([0-9]+(?:_[0-9]+)*)$/);
      if (m) {
        return { project: proj, taskId: m[1].toUpperCase() + "-" + m[2].replace(/_/g, ".") };
      }
    }
    return null;
  }

  function findLiveSession(projectName, taskId) {
    for (var i = 0; i < C.sessionsData.length; i++) {
      var s = C.sessionsData[i];
      var parsed = parseSessionTask(s.name);
      if (parsed && parsed.project === projectName && parsed.taskId.toUpperCase() === taskId.toUpperCase()) {
        return s;
      }
    }
    return null;
  }

  // ------------------------------------------------------------------
  // agentState badges (task-38 -- see server.py's get_agent_state/
  // record_agent_event, task-37)
  // ------------------------------------------------------------------

  var AGENT_BADGE_SPECS = {
    working: { className: "agent-badge agent-badge-working", text: "working" },
    waiting: { className: "agent-badge agent-badge-waiting", text: "waiting for input" },
    finished: { className: "agent-badge agent-badge-finished", text: "finished" },
    idle: {
      className: "agent-badge agent-badge-idle",
      text: "turn ended · may need input",
      title: "Codex reports the same turn-end event when it is done and when it is waiting for your chat reply. Attach to check."
    },
    "likely-finished": { className: "agent-badge agent-badge-likely-finished", text: "likely finished" },
    unknown: { className: "agent-badge agent-badge-unknown", text: "state unknown" }
  };

  // These states describe what a particular agent SESSION is doing, not a
  // durable task lifecycle. The server's event cache deliberately keeps the
  // last event by (project, task), so display must gate them on current tmux
  // liveness. Include task-62's forthcoming codex "idle" state now so it
  // cannot inherit this same residue bug when that badge lands.
  var SESSION_SCOPED_AGENT_STATES = { working: true, waiting: true, idle: true };

  // "unknown" plus a live session plus a branch task already marked Done
  // reads as an agent that finished without ever firing a hook (e.g. a
  // hookless custom agent, or one whose PostToolUse/Stop hook never
  // fired) -- distinct, less certain styling from a hook-confirmed
  // "finished" badge (see .agent-badge-likely-finished). `branchTask` is
  // only ever available where it was actually fetched (the drawer, via
  // /api/task) -- callers without it (session rows) just pass null and
  // get the raw state back, never guessing from a weaker signal like
  // main's own (possibly stale) task.status.
  function effectiveAgentState(rawState, live, branchTask) {
    var state = rawState || "unknown";
    if (!live && SESSION_SCOPED_AGENT_STATES[state]) return null;
    if (state === "unknown" && live && branchTask && branchTask.status === "Done") {
      return "likely-finished";
    }
    return state;
  }

  function renderAgentBadge(displayState, title) {
    var spec = AGENT_BADGE_SPECS[displayState] || AGENT_BADGE_SPECS.unknown;
    return C.h("span", { className: spec.className, text: spec.text, title: title || spec.title });
  }

  // task-120: End session is offered for EVERY live session. task-38
  // originally ran this off a positive allowlist of finished/idle/
  // likely-finished/waiting, which meant a live session reading
  // "unknown" got no End session at all, and (the harvest area being
  // suppressed while a session lives) no Merge and no throwaway action
  // either: a task with a branch, a dirty worktree and nothing
  // whatsoever to click. That is not exotic. Lifecycle badges are
  // in-memory, a server restart clears the event store, and an idle
  // agent emits nothing to rebuild one from, so a single restart can
  // leave every live session in exactly that state. The user reaching
  // for `tmux kill-session` is the escape hatch Centrale exists to
  // remove, and the server agrees: POST /api/end-session has never had
  // an agent-state gate.
  //
  // task-132: nor is it ever DISABLED for an agent state. task-120 kept
  // "working" as a disabled button explaining itself -- and working is
  // the one state in which a user actually needs the button: a spawn
  // by mistake, the wrong task, an agent looping or stuck in a long
  // tool call all happen mid-turn, a fresh spawn reports working within
  // seconds, and a long task stays there for an hour. On 2026-09-04
  // nine of fourteen live sessions had an unclickable End session. So
  // the protection against one stray click killing work in progress is
  // the two-click arm Merge, cleanup and Discard already use (see
  // harvest.js's handleEndSessionClick): the first click on a working
  // agent arms the button, whose label then carries this sentence's
  // gist in the open rather than in a tooltip, and the second click
  // within the window ends the session. Every other state, "unknown"
  // included, ends on one click.
  var END_SESSION_ARM_REASONS = {
    working: "This agent is mid-turn -- its last event was \"working\", so ending the" +
      " session now will kill work in progress. Click again to end it anyway."
  };

  // The reason a first click on End session ARMS rather than ends for
  // `displayState`, or null when one click ends the session. An
  // unrecognised state ends at once: only a state Centrale positively
  // knows to mean "busy" is worth a second click.
  function endSessionArmReason(displayState) {
    return END_SESSION_ARM_REASONS[displayState] || null;
  }

  // task-120: the sentence every action that cannot run while this
  // task's agent is alive is disabled with -- the merge and the two
  // throwaway actions (see renderDrawerLiveSessionActions). Same family
  // as externalCheckoutReason above: it names the obstacle, says what
  // the server would do with the click, and names the way out.
  // task-151: it says "will refuse them" rather than naming a status
  // code, because the three refusals do not share one shape -- Abandon
  // and Discard are refused outright by their live-session checks, while
  // Merge fails harvest's first gate and comes back as an ordinary
  // report -- and the sentence has to stay true if either shape changes.
  function liveSessionBlockReason(live) {
    return "A session for this task is still live: " + live.name +
      ". Merge, Abandon and Discard are disabled while an agent is running in the worktree" +
      " -- the server will refuse them, and Centrale will not delete a worktree" +
      " from under a live agent. End the session first (the button is right above).";
  }

  // The board's own copy of a project, by name -- the one lookup the
  // three functions below (and the drawer's refresh, task-126) all
  // start from. Always resolved against C.boardData rather than held,
  // so a caller that outlives a refetch reads the fresh project rather
  // than the object it was handed when it started. Null once a project
  // leaves projects.json mid-session.
  function findProject(projectName) {
    return (C.boardData.projects || []).find(function (p) { return p.name === projectName; }) || null;
  }

  function findTaskStatus(projectName, taskId) {
    var project = findProject(projectName);
    if (!project) return null;
    var task = (project.tasks || []).find(function (t) { return t.id === taskId; });
    return task ? task.status : null;
  }

  // Full task object (with title) for a dependency row, resolved from
  // already-loaded board data. Returns null when the task isn't on the
  // board -- e.g. it lives in backlog/completed/ and was excluded from
  // the board fetch -- callers show a fallback in that case.
  function findTask(projectName, taskId) {
    var project = findProject(projectName);
    if (!project) return null;
    return (project.tasks || []).find(function (t) { return t.id === taskId; }) || null;
  }

  // Other live sessions in the same project as (projectName, taskId) --
  // used to decide whether spawning here risks touching files another
  // active agent is already working on. Excludes a session for this
  // exact task (that case is the existing duplicate-session / "Session
  // live" disabled-button path, handled separately).
  function findOtherLiveSessionsInProject(projectName, taskId) {
    return C.sessionsData.filter(function (s) {
      var parsed = parseSessionTask(s.name);
      if (!parsed) return false;
      var proj = s.project || parsed.project;
      return proj === projectName && parsed.taskId.toUpperCase() !== String(taskId).toUpperCase();
    });
  }

  function describeSessionFiles(s) {
    if (!Array.isArray(s.files)) return s.name;
    if (s.files.length === 0) return s.name + " — no files touched yet";
    var total = typeof s.filesTotal === "number" ? s.filesTotal : s.files.length;
    var extra = total - s.files.length;
    var list = s.files.join(", ") + (extra > 0 ? (" (+" + extra + " more)") : "");
    return s.name + " touched: " + list;
  }

  // ------------------------------------------------------------------
  // Seam: what the other files reach for through window.Centrale.
  // ------------------------------------------------------------------

  C.TMUX_UNAVAILABLE_TOOLTIP = TMUX_UNAVAILABLE_TOOLTIP;
  C.assigneeLabel = assigneeLabel;
  C.claimedElsewhereLabel = claimedElsewhereLabel;
  C.claimedElsewhereMessage = claimedElsewhereMessage;
  C.compareTasks = compareTasks;
  C.computeColumns = computeColumns;
  C.describeSessionFiles = describeSessionFiles;
  C.effectiveAgentState = effectiveAgentState;
  C.endSessionArmReason = endSessionArmReason;
  C.externalCheckout = externalCheckout;
  C.externalCheckoutReason = externalCheckoutReason;
  C.externalMergeReason = externalMergeReason;
  C.externalWorkButton = externalWorkButton;
  C.findLiveSession = findLiveSession;
  C.findOtherLiveSessionsInProject = findOtherLiveSessionsInProject;
  C.findProject = findProject;
  C.findTask = findTask;
  C.findTaskStatus = findTaskStatus;
  C.firstAssignee = firstAssignee;
  C.getFilteredItems = getFilteredItems;
  C.isActiveStatus = isActiveStatus;
  C.isFirstColumnStatus = isFirstColumnStatus;
  C.isTmuxAvailable = isTmuxAvailable;
  C.liveSessionBlockReason = liveSessionBlockReason;
  C.milestoneChipLabel = milestoneChipLabel;
  C.parkedBranch = parkedBranch;
  C.parseSessionTask = parseSessionTask;
  C.renderAgentBadge = renderAgentBadge;
  C.renderMilestoneFilter = renderMilestoneFilter;
  C.spawnButtonLabel = spawnButtonLabel;
})(window.Centrale = window.Centrale || {});
