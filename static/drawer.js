// static/drawer.js -- the task drawer: detail, session, spawn and harvest areas
//
// One of the per-concern frontend files loaded in order by
// static/index.html (task-89); the seam between them is the single
// window.Centrale namespace object, taken here as C. See
// static/state.js for the two rules that seam follows.
(function (C) {
  "use strict";

  // ------------------------------------------------------------------
  // Drawer
  // ------------------------------------------------------------------

  function openDrawer(project, task) {
    C.currentDrawer = { project: project.name, id: task.id, summary: task };
    C.byId("drawer").classList.add("open");
    C.byId("drawer").setAttribute("aria-hidden", "false");
    C.byId("drawer-backdrop").classList.add("open");

    C.byId("drawer-title").textContent = task.title || "(untitled)";
    C.byId("drawer-id").textContent = project.name + " / " + task.id + " • " + (task.status || "");

    var metaRow = C.byId("drawer-meta-row");
    C.clearChildren(metaRow);
    if (task.priority) {
      metaRow.appendChild(C.h("span", { className: "priority-pill priority-" + task.priority, text: task.priority }));
    }
    metaRow.appendChild(C.h("span", { className: "label-chip", text: C.assigneeLabel(task) }));
    if (task.ready) {
      metaRow.appendChild(C.h("span", { className: "status-dot-ready", text: "ready" }));
    } else if (C.isFirstColumnStatus(project, task.status)) {
      metaRow.appendChild(C.h("span", { className: "badge-blocked", text: "blocked" }));
    }
    (task.labels || []).forEach(function (lbl) {
      metaRow.appendChild(C.h("span", { className: "label-chip", text: lbl }));
    });

    var body = C.byId("drawer-body");
    C.clearChildren(body);
    body.appendChild(C.h("div", { attrs: { id: "drawer-detail-loading" }, text: "Loading task details…" }));

    attachDrawerBodyFadeSync();
    renderDrawerSpawnArea();
    renderDrawerHarvestArea();
    renderDrawerSessionArea();
    C.syncDrawerPanePolling();

    fetchDrawerDetail(project, task.id, true);
  }

  // ------------------------------------------------------------------
  // task-126: the drawer body, kept current while it is open
  // ------------------------------------------------------------------
  //
  // The body -- description, acceptance criteria, notes, and the
  // branch-side copy stashed behind them -- comes from GET /api/task,
  // and used to be fetched exactly once, by the openDrawer above. An
  // agent ticking a criterion while you were reading the drawer changed
  // nothing on screen until you closed and reopened it: what you were
  // reading was the snapshot from the moment you opened it. So the same
  // request now rides the ordinary refresh tick for as long as a drawer
  // is open (refreshDrawerDetail below, called from C.doRefresh), and
  // this is the one function that sends it: open-time and refresh alike
  // go through the single /api/task call site in the whole frontend.
  //
  // `initial` marks the open-time call. It owns the two things that are
  // its alone: the "Loading task details…" placeholder it was sent
  // under, and the error message that replaces it when it fails. A
  // refresh call is a viewer mid-read, so it owns neither -- see the
  // payload check and the catch below.

  // Is this response still the one the drawer is waiting for? The seq
  // is bumped by every openDrawer AND by closeDrawer, so a request sent
  // under an older one belongs to a drawer that has since closed or
  // moved to another task; the project/id check says the same thing
  // directly, for anything that could ever move currentDrawer without
  // the seq.
  function drawerAwaits(seq, projectName, taskId) {
    return seq === C.drawerRequestSeq &&
      !!C.currentDrawer &&
      C.currentDrawer.project === projectName &&
      C.currentDrawer.id === taskId;
  }

  function fetchDrawerDetail(project, taskId, initial) {
    var seq = initial ? ++C.drawerRequestSeq : C.drawerRequestSeq;
    if (initial) C.drawerDetailPayload = null; // nothing of this task's body is on screen yet
    // Marked in flight under the seq that sent it, so a response that
    // has been superseded can never clear a marker a newer request set
    // -- and a refresh tick never overlaps the request already out.
    C.drawerDetailInFlightSeq = seq;
    fetch("/api/task?project=" + encodeURIComponent(project.name) + "&id=" + encodeURIComponent(taskId))
      .then(function (res) {
        return res.json().catch(function () { return null; }).then(function (data) {
          if (!res.ok) throw new Error((data && data.error) || ("HTTP " + res.status));
          return data;
        });
      })
      .then(function (data) {
        if (!drawerAwaits(seq, project.name, taskId)) return; // superseded: closed, or moved on
        var task2 = data && data.task ? data.task : null;
        if (!task2) throw new Error("malformed task-view response");
        // Byte-identical to what is already rendered -> touch no DOM at
        // all. Same rule renderBoardAndSessionsIfChanged follows for the
        // board (task-50), and for the same reason: rebuilding the body
        // under a viewer would re-fold the sections they just opened,
        // reset their scroll position and drop keyboard focus. On the
        // common quiet tick there is nothing to redraw, so nothing is.
        var payload = JSON.stringify(data);
        if (payload === C.drawerDetailPayload) return;
        C.drawerDetailPayload = payload;
        var branchTask = data.branchTask && data.branchTask.task ? data.branchTask.task : null;
        // task-38's "likely finished" fallback needs branchTask.status,
        // and only the drawer ever fetches it -- stash it on currentDrawer
        // so renderDrawerSessionArea() can read it without a call of its
        // own, then re-render that area now that it's available.
        C.currentDrawer.branchTask = branchTask;
        // task-93: the theater's task rail reads the description and
        // acceptance criteria from THIS response too -- stashing it
        // here is what keeps the rail free of a fetch of its own.
        C.currentDrawer.detail = task2;
        renderDrawerSessionArea();
        // task-116: and the action area, whose Resume-vs-Re-spawn
        // decision reads that same branch-side status. It ran once
        // already, before this fetch was even sent; this is the pass
        // that has the status to decide with. Both areas, because a
        // branch's Resume/Re-spawn are members of the harvest area's
        // secondary row (task-125) while the spawn area still owns
        // the branch-less and external-checkout cases.
        renderDrawerSpawnArea();
        renderDrawerHarvestArea();
        if (C.refreshTheaterRail) C.refreshTheaterRail(); // no-op unless the rail is on screen
        // task-127: a REFRESH is a viewer mid-read, so this rebuild
        // carries their scroll offset and focus across it; an open
        // starts at the top, as it always has.
        renderDrawerDetail(task2, project, branchTask, !initial);
      })
      .catch(function (err) {
        // A refresh that failed says nothing: the viewer is mid-read,
        // and the last good body is still the best answer we have. The
        // next tick tries again.
        if (!initial || seq !== C.drawerRequestSeq) return;
        var body = C.byId("drawer-body");
        C.clearChildren(body);
        body.appendChild(C.h("div", { attrs: { id: "drawer-detail-error" }, text: "Failed to load task details: " + (err.message || err) }));
      })
      .then(function () {
        if (C.drawerDetailInFlightSeq === seq) C.drawerDetailInFlightSeq = 0;
      });
  }

  // Called from C.doRefresh (static/api.js) on every refresh tick, and
  // from nowhere else: no timer of its own, so the pane's single-poller
  // contract is untouched, and there is nothing to stop when the drawer
  // closes -- with no drawer open this returns before it fetches.
  function refreshDrawerDetail() {
    if (!C.currentDrawer) return;
    if (C.drawerDetailInFlightSeq) return; // the open-time fetch, or a previous tick's, is still out
    // The project as the board has it NOW, not as it was when the
    // drawer opened. Gone from projects.json mid-session -> nothing to
    // re-read it against, so the last good body stays.
    var project = C.findProject(C.currentDrawer.project);
    if (!project) return;
    fetchDrawerDetail(project, C.currentDrawer.id, false);
  }

  // ------------------------------------------------------------------
  // task-97: self-announcing sections
  // ------------------------------------------------------------------

  // How much a section has to hold before it starts folded. The point
  // is only to stop one long section from burying the headers below it,
  // so a small ticket still opens with everything showing; a viewer's
  // own toggle always wins over these (see drawerSection).
  var AUTO_COLLAPSE_WORDS = 60;   // ~5 lines at the drawer's summary width
  var AUTO_COLLAPSE_ITEMS = 5;

  function wordCount(text) {
    var trimmed = String(text === null || text === undefined ? "" : text).trim();
    return trimmed === "" ? 0 : trimmed.split(/\s+/).length;
  }

  function pluralCount(n, noun) {
    return n + " " + noun + (n === 1 ? "" : "s");
  }

  function sectionStartsCollapsed(key, autoCollapse) {
    var stored = C.drawerSectionCollapsed[key];
    return typeof stored === "boolean" ? stored : !!autoCollapse;
  }

  // Every drawer section is built through this. The header says what
  // the section holds -- "2/6", "1", "214 words" -- without anyone
  // having to scroll to it, and folds the section's body away so a long
  // description can't push acceptance criteria, dependencies and notes
  // out of view (task-97; the drawer's body used to be measured at
  // 155px against 1113px of content with a live session below it).
  // Returns { section, body }: callers append exactly the content they
  // rendered before to `body`, so nothing about WHAT the drawer shows,
  // or in which order, changes here.
  function drawerSection(key, heading, count, autoCollapse) {
    var collapsed = sectionStartsCollapsed(key, autoCollapse);
    var section = C.h("div", {
      className: "drawer-section" + (collapsed ? " collapsed" : ""),
      attrs: { "data-section": key }
    });
    var bodyId = "drawer-section-" + key;
    var btn = C.h("button", {
      className: "drawer-section-toggle",
      attrs: {
        type: "button",
        "aria-expanded": collapsed ? "false" : "true",
        "aria-controls": bodyId
      }
    });
    btn.appendChild(C.h("span", { className: "drawer-section-caret", attrs: { "aria-hidden": "true" } }));
    btn.appendChild(C.h("span", { className: "drawer-section-name", text: heading }));
    if (count) btn.appendChild(C.h("span", { className: "drawer-section-count", text: count }));
    var head = C.h("h3", { className: "drawer-section-head" });
    head.appendChild(btn);
    var sectionBody = C.h("div", { className: "drawer-section-body", attrs: { id: bodyId } });
    btn.addEventListener("click", function () {
      var nowCollapsed = !section.classList.contains("collapsed");
      section.classList.toggle("collapsed", nowCollapsed);
      btn.setAttribute("aria-expanded", nowCollapsed ? "false" : "true");
      C.persistDrawerSectionCollapsed(key, nowCollapsed);
      syncDrawerBodyFade();
    });
    section.appendChild(head);
    section.appendChild(sectionBody);
    return { section: section, body: sectionBody };
  }

  // The fade at the bottom edge of the scrolling body: a cue that there
  // is more ticket below, and nothing more. Driven by a class rather
  // than painted unconditionally so a drawer whose content fits shows
  // no false bottom. Nothing here touches the pane poller.
  function syncDrawerBodyFade() {
    var body = C.byId("drawer-body");
    if (!body) return;
    var more = body.scrollHeight - body.clientHeight - body.scrollTop > 4;
    body.classList.toggle("has-more", more);
  }

  // One listener for the life of the page, attached the first time a
  // drawer opens (the body element itself is in the document shell and
  // is never replaced -- only its children are).
  function attachDrawerBodyFadeSync() {
    var body = C.byId("drawer-body");
    if (!body || body.getAttribute("data-fade-sync") === "1") return;
    body.setAttribute("data-fade-sync", "1");
    body.addEventListener("scroll", syncDrawerBodyFade);
    window.addEventListener("resize", syncDrawerBodyFade);
  }

  // A spawned agent's status/AC/notes updates are committed only on its
  // task/<id> branch until that branch is merged, so `task` (read from
  // main) can be stale for an active spawn. Only worth a separate drawer
  // section when the branch actually shows something main doesn't.
  function branchTaskDiffersFromMain(task, branchTask) {
    if (!branchTask) return false;
    if ((branchTask.status || "") !== (task.status || "")) return true;
    if ((branchTask.implementationNotes || "").trim() !== (task.implementationNotes || "").trim()) return true;
    var mainAc = task.acceptanceCriteria || [];
    var branchAc = branchTask.acceptanceCriteria || [];
    if (mainAc.length !== branchAc.length) return true;
    for (var i = 0; i < branchAc.length; i++) {
      if (!!(mainAc[i] && mainAc[i].checked) !== !!(branchAc[i] && branchAc[i].checked)) return true;
    }
    return false;
  }

  // ------------------------------------------------------------------
  // task-127: carrying the viewer's place across a rebuild
  // ------------------------------------------------------------------
  //
  // task-126 gave the drawer body the first of task-50's two layers: a
  // tick whose payload is identical returns before touching any DOM, so
  // the quiet case -- almost every tick -- disturbs nothing. This is the
  // second layer, for the tick that DOES carry a change. renderDrawerDetail
  // clears the body and builds a fresh set of children, so `scrollTop`
  // returns to 0 and whatever had keyboard focus is gone: an agent
  // ticking a criterion while you are reading paragraph nine bounces you
  // to the top. So the offset and the focus are read off the old body
  // before it is cleared and put back on the new one.
  //
  // The drawer body is one scroll container rather than the board's
  // several, so the offset is a single number instead of a map keyed by
  // status -- but it is restored the same way, CLAMPED to the new
  // content height, so a body that shrank settles at its new bottom
  // instead of holding an offset that no longer exists.

  // Focus needs a key that survives the rebuild, since the element
  // itself will not: the section toggles are keyed by their section's
  // `data-section` and the linked dependency rows by their `data-dep`,
  // both stable across a re-render. Focus that was somewhere in the
  // body with no such key -- and focus whose element the change removed
  // -- lands on the body itself rather than nowhere, which is what
  // keeps it from falling back to <body> and out of the drawer
  // entirely. Focus that was never in the body (the close button, the
  // spawn area, the board behind) returns null here and is left alone.
  function drawerBodyFocusKey(body) {
    var el = document.activeElement;
    var onToggle = false;
    for (; el && el !== body; el = el.parentNode) {
      if (!el.getAttribute) continue;
      var dep = el.getAttribute("data-dep");
      if (dep !== null) return "dep:" + dep;
      if (String(el.className || "").split(/\s+/).indexOf("drawer-section-toggle") !== -1) {
        onToggle = true;
      }
      var section = el.getAttribute("data-section");
      if (section !== null) return onToggle ? "toggle:" + section : "body";
    }
    return el === body ? "body" : null;
  }

  function findDrawerFocusTarget(body, key) {
    var i;
    if (key === "body") return body;
    if (key.indexOf("dep:") === 0) {
      var depId = key.slice(4);
      var rows = body.querySelectorAll("[data-dep]");
      for (i = 0; i < rows.length; i++) {
        if (rows[i].getAttribute("data-dep") === depId) return rows[i];
      }
      return null;
    }
    var sectionKey = key.slice("toggle:".length);
    var sections = body.querySelectorAll(".drawer-section[data-section]");
    for (i = 0; i < sections.length; i++) {
      if (sections[i].getAttribute("data-section") !== sectionKey) continue;
      return sections[i].querySelectorAll("button.drawer-section-toggle")[0] || null;
    }
    return null;
  }

  function captureDrawerBodyView(body) {
    return { scrollTop: body.scrollTop, focusKey: drawerBodyFocusKey(body) };
  }

  function restoreDrawerBodyView(body, view) {
    if (view.focusKey) {
      // The element is gone -> the body, never nothing: see above.
      // preventScroll because focusing scrolls the target into view,
      // which would overrule the offset restored on the next line;
      // a browser that ignores the option is corrected by it anyway.
      (findDrawerFocusTarget(body, view.focusKey) || body).focus({ preventScroll: true });
    }
    var maxScroll = Math.max(0, body.scrollHeight - body.clientHeight);
    body.scrollTop = Math.max(0, Math.min(view.scrollTop, maxScroll));
  }

  function renderDrawerDetail(task, project, branchTask, preserveView) {
    var body = C.byId("drawer-body");
    var view = preserveView ? captureDrawerBodyView(body) : null;
    C.clearChildren(body);

    // Milestone (task-86) -- the same chip the card carries, in its own
    // labelled row at the top of the summary. Omitted entirely for a
    // task without one, so those drawers look exactly as they did.
    // task-91: the title, like the card's chip -- resolved through the
    // project's board data, since `task` here came from /api/task (a
    // passthrough of `task view --json`, which carries only the id).
    var milestone = C.milestoneChipLabel(project.name, task);
    if (milestone) {
      var msSection = drawerSection("milestone", "Milestone", null, false);
      msSection.section.classList.add("drawer-milestone-section");
      msSection.body.appendChild(C.h("div", {
        className: "drawer-milestone-row",
        children: [C.h("span", { className: "milestone-chip", text: milestone })]
      }));
      body.appendChild(msSection.section);
    }

    // Description
    var descWords = wordCount(task.description);
    var descSection = drawerSection(
      "description",
      "Description",
      descWords ? pluralCount(descWords, "word") : null,
      descWords > AUTO_COLLAPSE_WORDS
    );
    if (descWords) {
      descSection.body.appendChild(C.h("div", { className: "drawer-text", text: task.description }));
    } else {
      descSection.body.appendChild(C.h("div", { className: "drawer-empty", text: "No description." }));
    }
    body.appendChild(descSection.section);

    // Acceptance criteria
    var ac = task.acceptanceCriteria || [];
    var acChecked = ac.filter(function (item) { return !!(item && item.checked); }).length;
    var acSection = drawerSection(
      "acceptanceCriteria",
      "Acceptance Criteria",
      ac.length ? acChecked + "/" + ac.length : null,
      ac.length > AUTO_COLLAPSE_ITEMS
    );
    if (ac.length === 0) {
      acSection.body.appendChild(C.h("div", { className: "drawer-empty", text: "None." }));
    } else {
      ac.forEach(function (item) {
        var row = C.h("label", { className: "ac-item" + (item.checked ? " checked" : "") });
        var cb = document.createElement("input");
        cb.type = "checkbox";
        cb.checked = !!item.checked;
        cb.disabled = true;
        row.appendChild(cb);
        row.appendChild(C.h("span", { text: item.text || "" }));
        acSection.body.appendChild(row);
      });
    }
    body.appendChild(acSection.section);

    // Dependencies
    var deps = task.dependencies || [];
    var depSection = drawerSection(
      "dependencies",
      "Dependencies",
      deps.length ? String(deps.length) : null,
      deps.length > AUTO_COLLAPSE_ITEMS
    );
    if (deps.length === 0) {
      depSection.body.appendChild(C.h("div", { className: "drawer-empty", text: "None." }));
    } else {
      deps.forEach(function (depId) {
        var depTask = C.findTask(project.name, depId);
        var status = depTask ? depTask.status : null;
        var rowClassName = "dep-item" + (depTask ? " dep-item-linked" : "");
        var row = C.h("div", {
          className: rowClassName,
          // data-dep (task-127): the id is what a focused row is found
          // again by after a refresh rebuilds the body. Only the linked
          // rows carry it, because only they can hold focus.
          attrs: depTask ? { tabindex: "0", role: "button", "data-dep": depId } : {}
        });
        row.appendChild(C.h("span", { className: "dep-id", text: depId }));
        row.appendChild(C.h("span", {
          className: "dep-title" + (depTask ? "" : " unknown"),
          text: depTask ? (depTask.title || "(untitled)") : "(not on board)"
        }));
        if (status) {
          row.appendChild(C.h("span", { className: "dep-status", text: status }));
        } else {
          row.appendChild(C.h("span", { className: "dep-status unknown", text: "unknown" }));
        }
        if (depTask) {
          row.addEventListener("click", function () { openDrawer(project, depTask); });
          row.addEventListener("keydown", function (e) {
            if (e.key === "Enter" || e.key === " ") {
              e.preventDefault();
              openDrawer(project, depTask);
            }
          });
        }
        depSection.body.appendChild(row);
      });
    }
    body.appendChild(depSection.section);

    // Implementation plan
    var planWords = wordCount(task.implementationPlan);
    if (planWords) {
      var planSection = drawerSection(
        "implementationPlan",
        "Implementation Plan",
        pluralCount(planWords, "word"),
        planWords > AUTO_COLLAPSE_WORDS
      );
      planSection.body.appendChild(C.h("div", { className: "drawer-text", text: task.implementationPlan }));
      body.appendChild(planSection.section);
    }

    // Implementation notes
    var notesWords = wordCount(task.implementationNotes);
    if (notesWords) {
      var notesSection = drawerSection(
        "implementationNotes",
        "Implementation Notes",
        pluralCount(notesWords, "word"),
        notesWords > AUTO_COLLAPSE_WORDS
      );
      notesSection.body.appendChild(C.h("div", { className: "drawer-text", text: task.implementationNotes }));
      body.appendChild(notesSection.section);
    }

    // Branch state: everything above reflects main, which won't see a
    // spawn's own status/AC/notes updates until its branch is merged.
    // Only shown when the branch actually differs from what main has.
    if (branchTaskDiffersFromMain(task, branchTask)) {
      var branchAc = branchTask.acceptanceCriteria || [];
      var branchChecked = branchAc.filter(function (item) { return !!(item && item.checked); }).length;
      // Never auto-folded whatever its size: this panel exists to say
      // that everything above it is stale, which is not a thing to hide
      // behind a caret. The viewer can still fold it by hand.
      var branchSection = drawerSection(
        "branch",
        "On agent branch (unmerged)",
        branchAc.length ? branchChecked + "/" + branchAc.length : null,
        false
      );
      branchSection.section.classList.add("drawer-branch-section");
      branchSection.body.appendChild(C.h("div", {
        className: "drawer-branch-hint",
        text: "main hasn't merged this branch yet, so the sections above are stale. This is what the agent has actually committed:"
      }));
      branchSection.body.appendChild(C.h("div", {
        className: "drawer-branch-status",
        children: [C.h("span", { className: "label-chip", text: "Status: " + (branchTask.status || "unknown") })]
      }));

      if (branchAc.length > 0) {
        branchAc.forEach(function (item) {
          var row = C.h("label", { className: "ac-item" + (item.checked ? " checked" : "") });
          var cb = document.createElement("input");
          cb.type = "checkbox";
          cb.checked = !!item.checked;
          cb.disabled = true;
          row.appendChild(cb);
          row.appendChild(C.h("span", { text: item.text || "" }));
          branchSection.body.appendChild(row);
        });
      }

      if (branchTask.implementationNotes && String(branchTask.implementationNotes).trim()) {
        branchSection.body.appendChild(C.h("h4", { text: "Implementation Notes" }));
        branchSection.body.appendChild(C.h("div", { className: "drawer-text", text: branchTask.implementationNotes }));
      }

      body.appendChild(branchSection.section);
    }

    // The bottom-edge scroll cue, last so it rides the end of the
    // content, and a first sync now that the body's height is known.
    body.appendChild(C.h("div", { className: "drawer-body-fade", attrs: { "aria-hidden": "true" } }));
    // Before the fade sync, which reads scrollTop: the offset has to be
    // the one the viewer will actually be looking at. An open renders
    // at the top -- set rather than assumed, so this says where a fresh
    // drawer starts instead of leaving it to whatever the cleared body
    // happened to clamp to.
    if (view) restoreDrawerBodyView(body, view); else body.scrollTop = 0;
    syncDrawerBodyFade();
  }

  // task-38: agentState badge + End session, shown whenever there's
  // something to say about this task's agent. task-63: transient states
  // (working/waiting/future codex idle) are session-scoped, so their
  // last-known event is suppressed once this task has no live session;
  // branch-derived interrupted/unmerged presentation then remains in charge.
  // currentDrawer.branchTask is only set once renderDrawerDetail's own
  // /api/task fetch resolves (see openDrawer), so this renders without
  // the "likely finished" fallback until that lands, then re-renders.
  function renderDrawerSessionArea() {
    if (!C.currentDrawer) return;
    var area = C.byId("drawer-session-area");
    C.clearChildren(area);

    var task = C.currentDrawer.summary || {};
    var projectName = C.currentDrawer.project;
    var taskId = C.currentDrawer.id;
    var live = C.findLiveSession(projectName, taskId);
    // /api/sessions is fetched immediately after spawn and reflects the
    // event-store clear done just before launch. Prefer that live-session
    // payload over the older board summary, or the previous session's
    // waiting/working value can flash until the next board refresh.
    var rawState = live ? (live.agentState || "unknown") : task.agentState;
    if (!live && (!rawState || rawState === "unknown")) return; // nothing to report

    var displayState = C.effectiveAgentState(rawState, live, C.currentDrawer.branchTask);
    if (!displayState) return; // stale session-scoped event, no live session
    area.appendChild(C.renderAgentBadge(displayState));

    if ((displayState === "waiting" || displayState === "idle") && live) {
      var attachCmd = "tmux attach -t " + live.name;
      var waitingRow = C.h("div", { className: "drawer-waiting-attach" });
      waitingRow.appendChild(C.h("code", { text: attachCmd }));
      var waitingCopyBtn = C.h("button", { className: "btn btn-sm", text: "Copy" });
      waitingCopyBtn.addEventListener("click", function () { C.copyText(attachCmd, waitingCopyBtn); });
      waitingRow.appendChild(waitingCopyBtn);
      area.appendChild(waitingRow);
    }

    // task-120: a LIVE session always gets an End session button -- the
    // one state that used to omit it ("unknown") is the state a server
    // restart leaves every running session in, and omitting it there
    // left the whole drawer with nothing to click. task-132: and it is
    // never disabled for the agent's state. A working agent's button
    // arms on the first click and ends on the second (see harvest.js's
    // handleEndSessionClick); while it is armed the full reason is
    // spelled out under it, since the drawer has the room the sidebar
    // row lacks.
    if (live) {
      var armReason = C.endSessionArmReason(displayState);
      var endBtn = C.renderEndSessionButton(projectName, taskId, "btn btn-danger-outline",
                                            false, armReason);
      endBtn.style.width = "100%";
      area.appendChild(endBtn);
      if (armReason && C.endSessionIsArmed(projectName, taskId)) {
        area.appendChild(C.h("div", { className: "spawn-status-line", text: armReason }));
      }
      var endLine = C.renderEndSessionStatusLine(C.endSessionStates[C.spawnKey(projectName, taskId)]);
      if (endLine) area.appendChild(endLine);
    }
  }

  // task-116: a branch with no live session whose own task is still in
  // an active status IS an interrupted task, whatever its worktree looks
  // like -- an agent killed a moment AFTER committing leaves a clean
  // worktree and a still-In-Progress task, and reading only
  // worktreeDirty called that finished and offered nothing but a fresh
  // Re-spawn (the 2026-09-03 tmux-server death stranded 22 spawns that
  // way). The status comes from the BRANCH's copy of the task, never
  // main's: main is pinned at the spawn's own claim commit for the whole
  // life of a spawn (task-22), so it says "In Progress" for a finished
  // branch too, and a merge candidate would read as interrupted.
  //
  // Only ever true once /api/task has actually landed
  // (currentDrawer.branchTask is undefined until then, see openDrawer's
  // fetch) -- until it does, the drawer keeps the older worktreeDirty-
  // only answer rather than claiming "resume" about a status it has not
  // read. renderDrawerSpawnArea is re-run when the response arrives.
  function drawerBranchStatusIsActive() {
    var drawer = C.currentDrawer;
    if (!drawer || !drawer.branchTask) return false;
    var project = (C.boardData.projects || []).find(function (p) {
      return p.name === drawer.project;
    });
    if (!project) return false;
    return C.isActiveStatus(project, drawer.branchTask.status);
  }

  // Drawer version of the same branch-lifecycle logic as the card (see
  // renderCard): a live session always wins (state 2); otherwise an
  // existing branch replaces the primary Spawn button with the subtler,
  // confirm-armed "Re-spawn agent" action (state 3) instead of offering
  // to start fresh; with neither, this is the ordinary Spawn button
  // (state 1), shown only while the task is actually ready -- so once a
  // branch merges back in (state 4), this naturally goes quiet again for
  // a task that's now Done, since a Done task is never "ready."
  function renderDrawerSpawnArea() {
    if (!C.currentDrawer) return;
    var area = C.byId("drawer-spawn-area");
    C.clearChildren(area);

    var task = C.currentDrawer.summary || {};
    var projectName = C.currentDrawer.project;
    var taskId = C.currentDrawer.id;
    var live = C.findLiveSession(projectName, taskId);

    // A just-succeeded merge means there's no worktree left to Re-spawn
    // or Resume into -- effectiveHasSpawnBranch hides this instantly,
    // same render pass, rather than waiting for the board's refetch.
    if (C.effectiveHasSpawnBranch(task, projectName, taskId) && !live) {
      // task-125: Resume and Re-spawn are members of the harvest area's
      // secondary row now, not this area's own stack -- a branch's
      // action area is one primary and ONE row of secondary actions,
      // and a row cannot span two sibling containers. This area is left
      // empty for a branch (so its divider doesn't draw), with one
      // exception: an externally checked-out branch, where the two
      // actions are replaced by an explanation rather than joining the
      // row.
      var ext = C.externalCheckout(task);
      if (ext) {
        // task-70: neither Resume nor Re-spawn can work -- git refuses
        // a second checkout of the branch -- so say so instead of
        // letting the click fail with a raw git error.
        renderDrawerExternalWork(area, ext);
      }
      return;
    }

    // A merge (or task-43 cleanup) just succeeded in this render pass
    // (same harvestStates/cleanupStates signals the card uses, see
    // justMerged/justCleanedUp a few lines above the card's Merge-
    // button block) -- the branch is gone and the task is presumably
    // Done, but currentDrawer.summary is still the stale pre-merge
    // snapshot (status In Progress, ready true) until the next board
    // refetch lands. Without this, the !task.ready check below reads
    // that stale ready=true and falls through to a Spawn button for a
    // task that just finished (task-41).
    var hKey = C.spawnKey(projectName, taskId);
    var hState = C.harvestStates[hKey];
    var cState = C.cleanupStates[hKey];
    var justMerged = !!(hState && hState.status === "success");
    var justCleanedUp = !!(cState && cState.status === "success");
    if (justMerged || justCleanedUp) return;

    if (!task.ready && !live) {
      return; // no branch, not ready (e.g. Done, or blocked) -- no actions
    }

    var key = C.spawnKey(projectName, taskId);
    var state = C.spawnStates[key];
    var display = C.spawnButtonDisplay(task, projectName, taskId, live, state);

    var btn = C.h("button", {
      className: "btn btn-primary" + (display.confirming ? " confirming" : ""),
      text: live ? "Session already running" : (display.longText || display.text)
    });
    btn.style.width = "100%";
    btn.disabled = display.disabled;
    if (display.noTmux) {
      btn.title = C.TMUX_UNAVAILABLE_TOOLTIP;
    } else if (live) {
      btn.title = "A session for this task is already live: " + live.name;
    } else if (display.confirming) {
      btn.title = display.confirmTitle || "";
    }
    btn.addEventListener("click", function () { C.handleSpawnClick(projectName, taskId); });
    area.appendChild(btn);

    if (live && (!state || state.status !== "success")) {
      var liveInfo = C.h("div", { className: "spawn-status-line success" });
      liveInfo.appendChild(document.createTextNode("Live session: " + live.name));
      var row = C.h("div", { className: "attach-row" });
      var cmd = "tmux attach -t " + live.name;
      row.appendChild(C.h("code", { text: cmd }));
      var cbtn = C.h("button", { className: "btn btn-sm", text: "Copy" });
      cbtn.addEventListener("click", function () { C.copyText(cmd, cbtn); });
      row.appendChild(cbtn);
      liveInfo.appendChild(row);
      area.appendChild(liveInfo);
    } else if (state && state.status === "error") {
      area.appendChild(C.h("div", { className: "spawn-status-line error", text: state.error }));
    } else if (state && state.status === "success") {
      area.appendChild(C.renderSpawnStatusLine(state));
    }
  }

  // The subtle secondary action for state 3 (branch awaiting merge, no
  // live session): sending an agent back into that same worktree/branch
  // to fix a failed safety gate or continue unfinished work -- distinct
  // from the primary Spawn button, which this replaces here, since that
  // one means starting fresh on a task with no work yet.
  // task-70: the drawer's explicit external-work state -- replaces the
  // Re-spawn/Resume action (the two it is mutually exclusive with, see
  // renderDrawerSpawnArea) with a disabled button carrying the same
  // honest reason POST /api/spawn and /api/resume would refuse with, and
  // a line naming the foreign checkout and the branch's last-commit age.
  function renderDrawerExternalWork(area, ext) {
    var btn = C.h("button", { className: "btn btn-sm", text: "Worked externally" });
    btn.disabled = true;
    btn.title = C.externalCheckoutReason(ext);
    area.appendChild(btn);
    area.appendChild(C.h("div", { className: "spawn-status-line", text: C.externalCheckoutReason(ext) }));
  }

  // `secondary` (task-116) is the Re-spawn that renders BESIDE a Resume
  // button rather than instead of one: quieter, and it leaves the
  // spawn-status line to the stronger action next to it, since both read
  // the same C.spawnStates entry and would otherwise print it twice.
  // The armed-confirm state is per-action (see spawn.js's armedFor), so
  // arming Resume does not light this one up.
  //
  // task-125: `row` takes the button and `area` takes the lines -- the
  // button is a member of the action area's one secondary row, and the
  // lines are full-width children of the area below it.
  function renderDrawerRespawnAction(row, area, projectName, taskId, secondary) {
    var key = C.spawnKey(projectName, taskId);
    var pending = C.spawnConfirmPending[key];
    var armed = C.armedFor(pending, "respawn");
    var state = C.spawnStates[key];
    var loading = state && state.status === "loading";

    var btn = C.h("button", {
      className: "btn btn-sm" + (secondary ? " btn-quiet" : "") + (armed ? " confirming" : ""),
      text: loading ? "Spawning…" : (armed ? "Confirm re-spawn?" : "Re-spawn agent")
    });
    btn.disabled = !C.isTmuxAvailable() || loading;
    btn.title = !C.isTmuxAvailable()
      ? C.TMUX_UNAVAILABLE_TOOLTIP
      : "Send an agent back into this task's existing worktree to fix a failed merge gate or continue unfinished work -- not a fresh start.";
    btn.addEventListener("click", function () { C.handleRespawnClick(projectName, taskId); });
    row.appendChild(btn);

    if (secondary) return;
    if (state && state.status === "error") {
      area.appendChild(C.h("div", { className: "spawn-status-line error", text: state.error }));
    } else if (state && state.status === "success") {
      area.appendChild(C.renderSpawnStatusLine(state));
    }
  }

  // The more specific action for an interrupted branch (no live session,
  // and either uncommitted changes in the worktree or a branch-side task
  // still in an active status -- see renderDrawerSpawnArea): continues
  // the dead session's own conversation (POST /api/resume) instead of
  // Re-spawn's fresh prompt, since there's a specific conversation to
  // pick back up. Same subtle, confirm-armed treatment as Re-spawn,
  // which renders beside it as the quieter alternative (task-116).
  // Same row/area split as Re-spawn above (task-125).
  function renderDrawerResumeAction(row, area, projectName, taskId) {
    var key = C.spawnKey(projectName, taskId);
    var pending = C.spawnConfirmPending[key];
    var armed = C.armedFor(pending, "resume");
    var state = C.spawnStates[key];
    var loading = state && state.status === "loading";

    var btn = C.h("button", {
      className: "btn btn-sm" + (armed ? " confirming" : ""),
      text: loading ? "Resuming…" : (armed ? "Confirm resume?" : "Resume agent")
    });
    btn.disabled = !C.isTmuxAvailable() || loading;
    btn.title = !C.isTmuxAvailable()
      ? C.TMUX_UNAVAILABLE_TOOLTIP
      : "Continue this task's interrupted agent session in its existing worktree -- picks up the same conversation where it left off, uncommitted changes included.";
    btn.addEventListener("click", function () { C.handleResumeClick(projectName, taskId); });
    row.appendChild(btn);

    if (state && state.status === "error") {
      area.appendChild(C.h("div", { className: "spawn-status-line error", text: state.error }));
    } else if (state && state.status === "success") {
      area.appendChild(C.renderSpawnStatusLine(state));
    }
  }

  // task-119: the two ways out of a bad attempt, drawer-only and
  // secondary by design -- Merge is the action this area is for, and
  // these sit under it, last in the secondary row (task-125). Both take
  // two clicks, the first of which measures what would be destroyed
  // (see harvest.js).
  //
  // Not offered for an alreadyMerged branch: the work is already on the
  // base there, "Merged -- clean up" above is the correct action, and a
  // forced delete of a merged branch would only be a noisier way to do
  // the same thing. Not offered while a session is live either -- the
  // whole harvest area is suppressed then, and End session is the way
  // out. An external checkout gets the disabled treatment every other
  // action on this page gives it, since the server would only 409.
  //
  // "Abandon worktree" is only meaningful when there IS a Centrale
  // worktree to abandon (branchCheckout.kind "centrale"); a parked
  // branch already has none. The discard is offered either way -- a
  // parked bad branch still needs deleting for the next spawn to start
  // from the base.
  // task-125: `row` takes the buttons, `area` the lines -- same split
  // as the two spawn actions that precede them in the row.
  function renderDrawerThrowawayActions(row, area, task, projectName, taskId) {
    if (task.alreadyMerged) return;
    var key = C.spawnKey(projectName, taskId);
    var ext = C.externalCheckout(task);
    var checkout = task.branchCheckout || null;
    var hasWorktree = !!(checkout && checkout.kind === "centrale");

    // Nothing at all for an external checkout, deliberately: the
    // primary action directly above is already rendered disabled with
    // that exact reason, and the spawn area below it spells the whole
    // sentence out again. A third disabled button in the same drawer
    // saying the same thing is noise, not honesty -- and the server
    // still refuses both routes with the 409 if anything reaches them.
    if (ext) return;

    // The board's branchCheckout catches up to a just-abandoned worktree
    // on the next refetch; until it does, the button that just succeeded
    // would still be offered and its click could only 404.
    var abandonState = C.abandonStates[key];
    var justAbandoned = !!(abandonState && abandonState.status === "success");

    if (hasWorktree && !justAbandoned) {
      var aDisplay = C.abandonButtonDisplay(projectName, taskId, abandonState);
      var abandonBtn = C.h("button", {
        className: "btn btn-sm btn-quiet" + (aDisplay.confirming ? " confirming" : ""),
        text: aDisplay.text
      });
      abandonBtn.disabled = aDisplay.disabled;
      abandonBtn.title = aDisplay.title || "";
      abandonBtn.addEventListener("click", function () {
        C.handleAbandonWorktreeClick(projectName, taskId);
      });
      row.appendChild(abandonBtn);
      if (aDisplay.detail) {
        area.appendChild(C.h("div", { className: "spawn-status-line", text: aDisplay.detail }));
      }
    }

    var dDisplay = C.discardButtonDisplay(projectName, taskId, C.discardStates[key]);
    var discardBtn = C.h("button", {
      // task-125: quiet at rest, exactly like the neighbours it sits
      // beside -- the first click only ARMS this action, so alarm
      // styling before anything is happening is styling that has
      // nothing to warn about, and it only trains the eye to skip the
      // red. The destructive treatment belongs to the armed state,
      // which is where the label names the commits and files a second
      // click would destroy.
      className: dDisplay.confirming
        ? "btn btn-sm btn-danger-outline confirming"
        : "btn btn-sm btn-quiet",
      text: dDisplay.text
    });
    discardBtn.disabled = dDisplay.disabled;
    discardBtn.title = dDisplay.title || "";
    discardBtn.addEventListener("click", function () {
      C.handleDiscardAttemptClick(projectName, taskId);
    });
    row.appendChild(discardBtn);
    if (dDisplay.detail) {
      area.appendChild(C.h("div", { className: "spawn-status-line", text: dDisplay.detail }));
    }
  }

  // task-125: the drawer's action area is TWO tiers -- the primary
  // action, and this one row under it holding every secondary action
  // the branch has. It used to stack three: a full-width primary, an
  // orphaned throwaway pill at a third of that width, and then a
  // separate group of spawn pills below a section divider -- three
  // unrelated-looking things rather than one set.
  //
  // Order in the row is DOM order, and DOM order is how often each
  // action is the right one: Resume (the interrupted conversation
  // back), Re-spawn (a fresh prompt in the same worktree), then the two
  // ways out of a bad attempt, with the destructive one last. Every
  // member is a btn-sm, so the row reads as one set of equal-weight
  // pills instead of a ladder of shrinking widths.
  //
  // Each action still prints its own status/detail lines into `area`
  // rather than into the row: those are full-width sentences belonging
  // under the whole row, not beside one button.
  function renderDrawerSecondaryRow(area, task, projectName, taskId, opts) {
    var row = C.h("div", { className: "drawer-action-row" });
    var live = opts.live;
    area.appendChild(row);

    // task-66's reconcile offer leads the row when it is there at all:
    // a merge blocked on a branch that is behind its base is the one
    // moment this is the action wanted most. It used to render on its
    // own line directly under the blocked reason it answers -- which
    // read as a third tier, and the reason is still immediately above.
    var rDisplay = opts.reconcile
      ? C.reconcileButtonDisplay(projectName, taskId, opts.reconcile)
      : null;
    if (rDisplay) {
      var reconcileBtn = C.h("button", {
        className: "btn btn-sm" + (rDisplay.confirming ? " confirming" : ""),
        text: rDisplay.text
      });
      reconcileBtn.disabled = rDisplay.disabled;
      reconcileBtn.title = rDisplay.title || "";
      reconcileBtn.addEventListener("click", function () { C.handleReconcileClick(projectName, taskId); });
      row.appendChild(reconcileBtn);
    }

    // The condition renderDrawerSpawnArea used to answer before these
    // two moved here: a branch that still exists, no live session in it,
    // and not checked out where Centrale cannot touch it -- that last
    // case gets the "Worked externally" explanation the spawn area
    // renders instead of a pair of actions that could only fail.
    if (C.effectiveHasSpawnBranch(task, projectName, taskId) && !live && !C.externalCheckout(task)) {
      if (task.worktreeDirty || drawerBranchStatusIsActive()) {
        // task-116: Resume leads -- it is the action that gets the
        // interrupted conversation back -- and Re-spawn stays reachable
        // beside it for the case where a fresh prompt really is what's
        // wanted. A Done branch (and one whose status hasn't been read
        // yet) keeps the Re-spawn-only shape it has always had: it is a
        // merge candidate, not a resume candidate.
        renderDrawerResumeAction(row, area, projectName, taskId);
        renderDrawerRespawnAction(row, area, projectName, taskId, true);
      } else {
        renderDrawerRespawnAction(row, area, projectName, taskId);
      }
    }

    // task-119: the two ways out come last -- Merge is what this area is
    // for, and these are what to do when it is not going to happen.
    // Never while a Merge POST of this task's own is still outstanding:
    // the gates are running against the very worktree and branch these
    // buttons would delete, and "Merging..." is not the moment to offer
    // "throw it away".
    if (!opts.branchGone && !opts.mergeInFlight && task.hasSpawnBranch) {
      renderDrawerThrowawayActions(row, area, task, projectName, taskId);
    }

    // The reconcile offer's own lines, below the row like every other
    // member's: what it would do, and the error a refused POST left.
    if (rDisplay) {
      if (rDisplay.detail) {
        area.appendChild(C.h("div", { className: "spawn-status-line", text: rDisplay.detail }));
      }
      var rState = C.spawnStates[C.spawnKey(projectName, taskId)];
      if (rState && rState.status === "error") {
        area.appendChild(C.h("div", { className: "spawn-status-line error", text: rState.error }));
      }
    }

    // Nothing to group: a merged branch mid-cleanup, a discard that just
    // landed. An empty row would only draw its own top margin.
    if (!row.childNodes.length) area.removeChild(row);
  }

  // task-120: what this area renders while a session is live. The rule
  // it replaces was "render nothing at all", which was defensible on
  // its own -- acting on a worktree an agent is running in would only
  // 409 -- and became a dead end the moment the session area also had
  // nothing to offer (a live session in "unknown", see tasks.js's
  // endSessionArmReason): a task with a branch, a dirty worktree,
  // and no control anywhere in the drawer. The actions are still
  // unreachable; they are now unreachable VISIBLY, disabled and
  // carrying the sentence that names the session and points at End
  // session above.
  //
  // Same shape as the live-less area right below, minus every click:
  // the primary is "Merged -- clean up" on an already-merged branch and
  // "Merge" otherwise, and the throwaway actions follow the same rules
  // renderDrawerThrowawayActions applies -- none on a merged branch
  // (cleanup is the right action there), and no "Abandon worktree" when
  // there is no Centrale worktree to abandon.
  function renderDrawerLiveSessionActions(area, task, live) {
    var reason = C.liveSessionBlockReason(live);
    function blocked(text, className) {
      var btn = C.h("button", { className: className, text: text });
      btn.disabled = true;
      btn.title = reason;
      return btn;
    }

    var primary = blocked(task.alreadyMerged ? "Merged — clean up" : "Merge", "btn btn-primary");
    primary.style.width = "100%";
    area.appendChild(primary);

    if (!task.alreadyMerged) {
      // task-125: the same two tiers as the live-less area below --
      // primary, then one row -- so nothing about the grouping changes
      // when a session starts. Both are quiet here for the same reason
      // the discard is quiet at rest: neither is armed, and one of them
      // is disabled besides.
      var row = C.h("div", { className: "drawer-action-row" });
      var checkout = task.branchCheckout || null;
      if (checkout && checkout.kind === "centrale") {
        row.appendChild(blocked("Abandon worktree, keep branch", "btn btn-sm btn-quiet"));
      }
      row.appendChild(blocked("Discard attempt", "btn btn-sm btn-quiet"));
      area.appendChild(row);
    }

    // The reason in the open, not only in a tooltip: a disabled button
    // whose explanation needs a hover is barely better than no button.
    area.appendChild(C.h("div", { className: "spawn-status-line", text: reason }));
  }

  function renderDrawerHarvestArea() {
    if (!C.currentDrawer) return;
    var area = C.byId("drawer-harvest-area");
    C.clearChildren(area);

    var task = C.currentDrawer.summary || {};
    var projectName = C.currentDrawer.project;
    var taskId = C.currentDrawer.id;
    var key = C.spawnKey(projectName, taskId);
    var state = C.harvestStates[key];
    var report = (state && state.report) || {};
    var divergence = report.doneDivergence;
    var discardable = report.discardableMainTaskEdit;
    var cState = C.cleanupStates[key];
    var dState = C.discardStates[key];
    var abState = C.abandonStates[key];
    var justMerged = !!(state && state.status === "success");
    var justCleanedUp = !!(cState && cState.status === "success");
    // task-119: a finished discard/abandon behaves like a finished
    // merge here -- the clickable actions go instantly, and the
    // outcome line stays. It has to stay through the board refetch
    // that follows, because the discard's line is where the recovery
    // command lives: the toast carrying it expires after six seconds,
    // and the branch it recovers is already gone.
    var justDiscarded = !!(dState && dState.status === "success");
    var justAbandoned = !!(abState && abState.status === "success");
    // Two different questions, and conflating them would take Merge away
    // from a branch that is still perfectly mergeable. `branchGone` is
    // what silences the actions: a merge, a cleanup and a discard all
    // end with no branch. An ABANDON keeps the branch -- parked and
    // still mergeable is the entire point of it -- so it only counts as
    // `settled`, which is the weaker claim that keeps this area (and
    // its outcome line) on screen.
    var branchGone = justMerged || justCleanedUp || justDiscarded;
    var settled = branchGone || justAbandoned;

    // Same reasoning as the card: only an unmerged task/<id> branch
    // makes the Merge/cleanup button meaningful, whatever main's status
    // says (see the "Re-spawn agent" action above for what to do with a
    // live session that needs to go back to work). The RAW
    // hasSpawnBranch, not the effective one, so a just-succeeded merge/
    // cleanup's confirmation line keeps showing through the stale-
    // until-refetch window -- only the clickable button itself needs to
    // disappear instantly.
    if (!(task.hasSpawnBranch || settled)) return;

    // task-120: a live session no longer empties this area, it disables
    // it. `settled` is the just-acted window, where the outcome line
    // below is the whole point of the render and there is nothing left
    // to block anyway.
    var liveSession = C.findLiveSession(projectName, taskId);
    if (liveSession && !settled) {
      renderDrawerLiveSessionActions(area, task, liveSession);
      return;
    }

    if (!branchGone) {
      // task-43: a fully-merged branch means Merge would only ever
      // refuse -- offer the informed cleanup action instead.
      var ext = C.externalCheckout(task);
      if (task.alreadyMerged && ext) {
        // task-80: same rule as the card footer -- an out-of-band merge
        // whose foreign checkout is still around gets the disabled
        // "Worked externally" button (reason as tooltip) instead of a
        // cleanup click the server would only refuse with 409. The spawn
        // area right below already spells the reason out in full.
        var extBtn = C.externalWorkButton(ext, "btn btn-primary");
        extBtn.style.width = "100%";
        area.appendChild(extBtn);
      } else if (task.alreadyMerged) {
        var cDisplay = C.cleanupButtonDisplay(projectName, taskId, cState);
        var cleanupBtn = C.h("button", {
          className: "btn btn-primary" + (cDisplay.confirming ? " confirming" : ""),
          text: cDisplay.text
        });
        cleanupBtn.style.width = "100%";
        cleanupBtn.disabled = cDisplay.disabled;
        cleanupBtn.title = cDisplay.confirming
          ? cDisplay.confirmTitle
          : "This branch is already fully merged -- remove its worktree and delete the branch.";
        cleanupBtn.addEventListener("click", function () { C.handleCleanupClick(projectName, taskId); });
        area.appendChild(cleanupBtn);
      } else if (divergence) {
        var adoptDisplay = C.adoptDoneButtonDisplay(
          projectName, taskId, state, divergence
        );
        var adoptBtn = C.h("button", {
          className: "btn btn-primary" + (adoptDisplay.confirming ? " confirming" : ""),
          text: adoptDisplay.text
        });
        adoptBtn.style.width = "100%";
        adoptBtn.disabled = adoptDisplay.disabled;
        adoptBtn.title = adoptDisplay.title;
        adoptBtn.addEventListener("click", function () {
          C.handleAdoptDoneClick(projectName, taskId);
        });
        area.appendChild(adoptBtn);
        if (adoptDisplay.detail) {
          area.appendChild(C.h("div", {
            className: "spawn-status-line",
            text: adoptDisplay.detail
          }));
        }
      } else if (discardable) {
        var discardDisplay = C.discardMainButtonDisplay(
          projectName, taskId, state, discardable
        );
        var discardBtn = C.h("button", {
          className: "btn btn-primary" + (discardDisplay.confirming ? " confirming" : ""),
          text: discardDisplay.text
        });
        discardBtn.style.width = "100%";
        discardBtn.disabled = discardDisplay.disabled;
        discardBtn.title = discardDisplay.title;
        discardBtn.addEventListener("click", function () {
          C.handleDiscardMainClick(projectName, taskId);
        });
        area.appendChild(discardBtn);
        if (discardDisplay.detail) {
          area.appendChild(C.h("div", {
            className: "spawn-status-line",
            text: discardDisplay.detail
          }));
        }
      } else {
        // task-81: the primary Merge is disabled, with the external
        // reason as its title, while the branch is checked out outside
        // Centrale -- it sits right above the disabled "Worked
        // externally" spawn action and says the same thing. No click
        // handler in that case (see harvestButtonDisplay).
        var display = C.harvestButtonDisplay(state, task);
        var btn = C.h("button", { className: "btn btn-primary", text: display.text });
        btn.style.width = "100%";
        btn.disabled = display.disabled;
        btn.title = display.title;
        if (!display.external) {
          btn.addEventListener("click", function () { C.harvestTask(projectName, taskId); });
        }
        area.appendChild(btn);
      }
    }

    if (state && (state.status === "error" || state.status === "success" || state.status === "blocked")) {
      area.appendChild(C.renderHarvestStatusLine(state));
    }

    if (cState && (cState.status === "error" || cState.status === "success")) {
      area.appendChild(C.renderCleanupStatusLine(cState));
    }

    // task-125: the second tier -- Resume, Re-spawn and the two
    // throwaway actions in one row under the primary. The throwaways'
    // outcome lines outlive their buttons (see `settled` above), which
    // is what keeps the recovery command on screen after the board
    // refetch removes the branch.
    var mergeInFlight = !!(state && state.status === "loading");
    renderDrawerSecondaryRow(area, task, projectName, taskId, {
      live: liveSession,
      branchGone: branchGone,
      mergeInFlight: mergeInFlight,
      // task-66: the reconcile offer, absent whenever the report has no
      // behindBase (the branch is up to date, or the failure is at an
      // earlier gate). Merge stays exactly as it was above; this is
      // purely additive.
      reconcile: (state && state.status === "blocked" && report.behindBase && !branchGone)
        ? report.behindBase
        : null
    });
    if (abState && (abState.status === "error" || abState.status === "success")) {
      area.appendChild(C.renderAbandonStatusLine(abState));
    }
    if (dState && (dState.status === "error" || dState.status === "success")) {
      area.appendChild(C.renderDiscardStatusLine(dState));
    }
  }

  // ------------------------------------------------------------------
  // Seam: what the other files reach for through window.Centrale.
  // ------------------------------------------------------------------

  C.openDrawer = openDrawer;
  C.refreshDrawerDetail = refreshDrawerDetail;
  C.renderDrawerHarvestArea = renderDrawerHarvestArea;
  C.renderDrawerSessionArea = renderDrawerSessionArea;
  C.renderDrawerSpawnArea = renderDrawerSpawnArea;
})(window.Centrale = window.Centrale || {});
