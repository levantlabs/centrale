// static/board.js -- the sidebar chips, the error banners and the board/column/card render
//
// One of the per-concern frontend files loaded in order by
// static/index.html (task-89); the seam between them is the single
// window.Centrale namespace object, taken here as C. See
// static/state.js for the two rules that seam follows.
(function (C) {
  "use strict";

  // ------------------------------------------------------------------
  // Rendering: header chips + error banners
  // ------------------------------------------------------------------

  // Solos `name` (shows only it); if it's already the sole active
  // project, restores all instead. Plain click / Enter on a row.
  function soloOrRestoreProject(name) {
    if (C.activeProjects.size === 1 && C.activeProjects.has(name)) {
      C.activeProjects = new Set(C.knownProjects);
    } else {
      C.activeProjects = new Set([name]);
    }
    C.renderAll();
  }

  // Toggles just `name` on/off without touching any other project's
  // selection. Ctrl/Cmd+click, Ctrl/Cmd+Enter, or the row's checkbox.
  function toggleOneProject(name) {
    if (C.activeProjects.has(name)) C.activeProjects.delete(name);
    else C.activeProjects.add(name);
    C.renderAll();
  }

  // task-54: same case-insensitive "done" comparison the codebase
  // already standardized server-side (spawn._check_not_done,
  // server._branch_already_merged: str(status).strip().lower() ==
  // "done") -- an unknown/custom/missing status counts as open (fails
  // in the informative direction, never silently hidden from the
  // sidebar count).
  function isDoneStatus(status) {
    return String(status || "").trim().toLowerCase() === "done";
  }

  function renderProjectChips() {
    C.persistActiveProjects();

    var container = C.byId("project-chips");
    C.clearChildren(container);
    C.byId("projects-count").textContent = String(C.knownProjects.length);

    var resetAllBtn = C.byId("projects-reset-all");
    var filtered = !!C.activeProjects && C.activeProjects.size < C.knownProjects.length;
    resetAllBtn.hidden = !filtered;

    if (C.knownProjects.length === 0) {
      container.appendChild(C.h("div", { className: "sidebar-empty", text: "No projects configured" }));
      return;
    }

    C.knownProjects.forEach(function (name) {
      var project = (C.boardData.projects || []).find(function (p) { return p.name === name; });
      var isActive = C.activeProjects && C.activeProjects.has(name);
      var hasError = project && project.error;
      var style = C.projectChipStyle(name);
      // task-54: the badge itself is an OPEN-task count (not the total,
      // which grows monotonically and mostly measures history once Done
      // tasks pile up) -- the full breakdown lives in the badge's own
      // hover title instead.
      var tasks = project && Array.isArray(project.tasks) ? project.tasks : [];
      var openCount = 0, doneCount = 0;
      tasks.forEach(function (t) {
        if (isDoneStatus(t.status)) doneCount++; else openCount++;
      });
      var countTitle = openCount + " open · " + doneCount + " done";
      var behaviorHint = "Click to show only this project (click the solo'd project again to show all). "
        + "Ctrl/Cmd+click, or the checkbox, to toggle just this one on/off.";
      var titleText = (hasError ? ("Error: " + project.error + " — ") : (project ? (project.path + " — ") : ""))
        + behaviorHint;

      // A <div role="button"> rather than a <button> here, since it hosts
      // a nested checkbox and "open board" <button> — buttons can't nest
      // in HTML, and a checkbox inside a <button> isn't valid either.
      var rowClick = function (e) {
        if (e.ctrlKey || e.metaKey) toggleOneProject(name);
        else soloOrRestoreProject(name);
      };
      var chip = C.h("div", {
        className: "chip chip-btn" + (isActive ? " active" : ""),
        title: titleText,
        attrs: { role: "button", tabindex: "0" },
        onclick: rowClick
      });
      chip.addEventListener("keydown", function (e) {
        if (e.key !== "Enter") return; // Space is the checkbox's, handled there natively
        e.preventDefault();
        if (e.ctrlKey || e.metaKey) toggleOneProject(name);
        else soloOrRestoreProject(name);
      });

      var checkbox = document.createElement("input");
      checkbox.type = "checkbox";
      checkbox.className = "chip-checkbox";
      checkbox.checked = !!isActive;
      checkbox.setAttribute("aria-label", "Show " + name);
      checkbox.addEventListener("click", function (e) {
        e.stopPropagation(); // don't also trigger the row's solo/toggle
        toggleOneProject(name);
      });
      chip.appendChild(checkbox);

      var swatch = C.h("span", {});
      swatch.style.width = "7px";
      swatch.style.height = "7px";
      swatch.style.borderRadius = "50%";
      swatch.style.background = style.color;
      swatch.style.flex = "none";
      swatch.style.display = "inline-block";
      chip.appendChild(swatch);
      chip.appendChild(C.h("span", { className: "proj-name", text: name }));
      chip.appendChild(C.h("span", { className: "proj-count", text: String(openCount), title: countTitle }));
      if (hasError) {
        chip.appendChild(C.h("span", { className: "warn-mark", text: "⚠" }));
      }
      var openBtn = C.h("button", {
        className: "chip-open-board",
        text: "⧉",
        title: "Open Backlog.md board for " + name,
        attrs: { type: "button", "aria-label": "Open board for " + name }
      });
      openBtn.addEventListener("click", function (e) {
        e.stopPropagation();
        C.openProjectBoard(name, openBtn);
      });
      chip.appendChild(openBtn);
      container.appendChild(chip);
    });
  }

  function renderErrorBanners() {
    var globalErr = C.byId("global-error");
    if (C.lastBoardError) {
      globalErr.hidden = false;
      globalErr.textContent = "Failed to reach server: " + C.lastBoardError;
    } else {
      globalErr.hidden = true;
      C.clearChildren(globalErr);
    }

    var projErr = C.byId("project-errors");
    C.clearChildren(projErr);
    var errored = (C.boardData.projects || []).filter(function (p) { return p.error; });
    if (errored.length === 0) {
      projErr.hidden = true;
    } else {
      projErr.hidden = false;
      errored.forEach(function (p) {
        var row = C.h("div", { className: "row" });
        row.appendChild(C.h("b", { text: p.name }));
        row.appendChild(document.createTextNode(" — " + p.error));
        projErr.appendChild(row);
      });
    }

    renderTmuxHintBanner();
  }

  var TMUX_HINT_DISMISSED_KEY = "centrale-tmux-hint-dismissed";

  function isTmuxHintDismissed() {
    try {
      return window.localStorage.getItem(TMUX_HINT_DISMISSED_KEY) === "1";
    } catch (e) {
      return false;
    }
  }

  function dismissTmuxHint() {
    try { window.localStorage.setItem(TMUX_HINT_DISMISSED_KEY, "1"); } catch (e) { /* ignore */ }
    C.byId("tmux-hint-banner").hidden = true;
  }

  function renderTmuxHintBanner() {
    var banner = C.byId("tmux-hint-banner");
    if (C.isTmuxAvailable() || isTmuxHintDismissed()) {
      banner.hidden = true;
      return;
    }
    banner.hidden = false;
    var text = C.byId("tmux-hint-text");
    C.clearChildren(text);
    text.appendChild(document.createTextNode(
      "tmux isn't installed, so spawning agents is disabled — the board, drawer, and open-board button still work. Install it with "
    ));
    text.appendChild(C.h("code", { text: "sudo apt install tmux" }));
    text.appendChild(document.createTextNode(" and restart Centrale to enable spawning."));
  }

  // ------------------------------------------------------------------
  // Rendering: board / columns / cards
  // ------------------------------------------------------------------

  // task-50: clearChildren(boardEl) below always resets every lane's
  // scrollTop to 0 -- these capture/restore each .column-body's scroll
  // position keyed by its status (a data-status attribute set by
  // renderColumn, not DOM index/position, since columns can reorder or
  // appear/disappear -- e.g. the synthesized "Other" bucket -- between
  // rebuilds) so a user scrolled down a lane isn't yanked back to the
  // top by a rebuild. Restored clamped to the new scrollable range, so
  // a lane that shrank (fewer cards after this rebuild) doesn't jump to
  // a scrollTop past its new max.
  function captureColumnScrollPositions(boardEl) {
    var positions = {};
    boardEl.querySelectorAll(".column-body[data-status]").forEach(function (el) {
      positions[el.getAttribute("data-status")] = el.scrollTop;
    });
    return positions;
  }

  function restoreColumnScrollPositions(boardEl, positions) {
    boardEl.querySelectorAll(".column-body[data-status]").forEach(function (el) {
      var status = el.getAttribute("data-status");
      if (!Object.prototype.hasOwnProperty.call(positions, status)) return;
      var maxScroll = Math.max(0, el.scrollHeight - el.clientHeight);
      el.scrollTop = Math.max(0, Math.min(positions[status], maxScroll));
    });
  }

  function renderBoard() {
    var boardEl = C.byId("board");
    var emptyHint = C.byId("empty-hint");
    var loadingHint = C.byId("loading-hint");

    if (!C.firstLoadDone) {
      loadingHint.hidden = false;
      emptyHint.hidden = true;
      boardEl.hidden = true;
      return;
    }
    loadingHint.hidden = true;

    if (!C.boardData.projects || C.boardData.projects.length === 0) {
      emptyHint.hidden = false;
      boardEl.hidden = true;
      return;
    }
    emptyHint.hidden = true;
    boardEl.hidden = false;

    var savedScroll = captureColumnScrollPositions(boardEl);
    C.clearChildren(boardEl);

    var columns = C.computeColumns(C.boardData.projects);
    var items = C.getFilteredItems();
    var buckets = {};
    columns.forEach(function (c) { buckets[c] = []; });
    var otherBucket = [];
    items.forEach(function (item) {
      if (buckets[item.task.status]) buckets[item.task.status].push(item);
      else otherBucket.push(item);
    });

    var allColumns = columns.slice();
    if (otherBucket.length > 0) {
      allColumns.push("Other");
      buckets["Other"] = otherBucket;
    }

    allColumns.forEach(function (colName) {
      var colItems = buckets[colName] || [];
      colItems.sort(C.compareTasks);
      boardEl.appendChild(renderColumn(colName, colItems));
    });

    restoreColumnScrollPositions(boardEl, savedScroll);
  }

  function renderColumn(name, items) {
    var header = C.h("div", { className: "column-header", children: [
      C.h("span", { className: "name", text: name }),
      C.h("span", { className: "count", text: String(items.length) })
    ] });

    var body = C.h("div", { className: "column-body", attrs: { "data-status": name } });
    if (items.length === 0) {
      body.appendChild(C.h("div", { className: "column-empty", text: "No tasks" }));
    } else {
      items.forEach(function (item) {
        body.appendChild(renderCard(item.task, item.project));
      });
    }

    return C.h("div", { className: "column", children: [header, body] });
  }

  function renderCard(task, project) {
    var card = C.h("div", {
      className: "card",
      attrs: { tabindex: "0", role: "button" }
    });
    card.addEventListener("click", function () { C.openDrawer(project, task); });
    card.addEventListener("keydown", function (e) {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        C.openDrawer(project, task);
      }
    });

    if (task.id) {
      card.appendChild(C.h("div", { className: "card-id", text: task.id }));
    }
    card.appendChild(C.h("div", { className: "card-title", text: task.title || "(untitled)" }));

    var meta = C.h("div", { className: "card-meta" });
    var pStyle = C.projectChipStyle(project.name);
    var pChip = C.h("span", { className: "proj-chip", text: project.name });
    C.applyStyle(pChip, pStyle);
    meta.appendChild(pChip);

    if (task.priority) {
      meta.appendChild(C.h("span", {
        className: "priority-pill priority-" + task.priority,
        text: task.priority
      }));
    }

    var firstCol = C.isFirstColumnStatus(project, task.status);
    if (task.ready) {
      meta.appendChild(C.h("span", { className: "status-dot-ready", text: "ready" }));
    } else if (firstCol) {
      meta.appendChild(C.h("span", {
        className: "badge-blocked",
        text: "blocked",
        title: "Not ready — dependencies are unmet"
      }));
    }
    // A live session, and an "effective" hasSpawnBranch that already
    // reflects a just-succeeded merge even before the board's next
    // refetch lands (see effectiveHasSpawnBranch) -- both computed once,
    // up front, since this badge and the action block below (see "Card
    // actions follow the branch lifecycle") all need them.
    var live = C.findLiveSession(project.name, task.id);
    var effBranch = C.effectiveHasSpawnBranch(task, project.name, task.id);
    var ext = C.externalCheckout(task);
    if (effBranch && ext && !live) {
      // task-70: the branch was adopted into a worktree Centrale doesn't
      // manage; there is no Centrale worktree, so "interrupted" can't
      // apply and "unmerged branch" would hide what's really happening.
      meta.appendChild(C.h("span", {
        className: "badge-external",
        text: "worked externally",
        title: C.externalCheckoutReason(ext)
      }));
    } else if (effBranch && task.worktreeDirty && !live) {
      // task-116 deliberately left this badge on worktreeDirty alone,
      // where the drawer's Resume offer no longer is. The drawer widened
      // its rule by reading the BRANCH's copy of the task, and the board
      // has no such copy: producing one costs a `backlog task view`
      // subprocess (sometimes a detached snapshot too -- see server.py's
      // _branch_task_view) per branch-bearing task, on every poll, where
      // the drawer pays it once per open. The board's own copy is not a
      // substitute: a spawn commits its claim to main (status "In
      // Progress") and main then stays pinned there for the branch's
      // whole life, so keying the badge on it would call every finished,
      // awaiting-merge branch "interrupted" -- exactly the distinction
      // this badge exists to make. So a committed-then-killed task's
      // card reads "unmerged branch", which is true, and the drawer --
      // the only surface that has the branch status, and the only one
      // carrying the actions anyway -- is where it becomes resumable.
      meta.appendChild(C.h("span", {
        className: "badge-interrupted",
        text: "interrupted",
        title: "This task's agent session ended without finishing -- uncommitted changes are still sitting in its worktree. Open the drawer to Resume it."
      }));
    } else if (effBranch && C.parkedBranch(task) && !live) {
      // task-70: distinct from a plain unmerged branch -- checked out
      // nowhere at all, its worktree already removed.
      meta.appendChild(C.h("span", {
        className: "status-dot-branch",
        text: "parked branch",
        title: "A task/<id> branch exists for this task but is checked out nowhere -- its worktree is gone. Its own status/ACs/notes may be ahead of what's shown here until it's merged"
      }));
    } else if (effBranch) {
      meta.appendChild(C.h("span", {
        className: "status-dot-branch",
        text: "unmerged branch",
        title: "An agent's task/<id> branch exists for this task -- its own status/ACs/notes may be ahead of what's shown here until it's merged"
      }));
    }
    card.appendChild(meta);

    // task-86: the milestone chip leads the same chips row the labels
    // already use (accent-tinted and pill-shaped -- see .milestone-chip
    // -- so it never reads as just another label). A task with no
    // milestone renders exactly the DOM it did before: the row is still
    // built only when there is something to put in it.
    // task-91: the chip carries the milestone's TITLE ("post-0.2.0"),
    // falling back to its id only when the title can't be resolved.
    var milestone = C.milestoneChipLabel(project.name, task);
    var cardLabels = task.labels || [];
    if (milestone || cardLabels.length > 0) {
      var labelsRow = C.h("div", { className: "card-labels" });
      if (milestone) {
        labelsRow.appendChild(C.h("span", {
          className: "milestone-chip",
          text: milestone,
          title: "Milestone: " + milestone
        }));
      }
      cardLabels.forEach(function (lbl) {
        labelsRow.appendChild(C.h("span", { className: "label-chip", text: lbl }));
      });
      card.appendChild(labelsRow);
    }

    // Card actions follow the branch lifecycle -- never both Spawn and
    // Merge at once, since spawning fresh onto a task that already has
    // finished work waiting to merge would be the wrong action:
    //   1. no branch, no live session -> Spawn (if ready)
    //   2. a live session -> "Session live" (disabled), regardless of
    //      whether a branch also exists yet (still in progress)
    //   3. a branch, no live session -> Merge only (awaiting merge; a
    //      fresh Spawn isn't offered here -- see the drawer's "Re-spawn
    //      agent" for deliberately sending an agent back into it)
    //   4. after merge (no branch again) -> back to Spawn if ready, or
    //      nothing once the task is actually Done
    if (task.ready && (live || !effBranch)) {
      var key = C.spawnKey(project.name, task.id);
      var state = C.spawnStates[key];
      var display = C.spawnButtonDisplay(task, project.name, task.id, live, state);

      var footer = C.h("div", { className: "card-footer" });
      var spawnBtn = C.h("button", {
        className: "btn btn-sm spawn-inline" + (display.confirming ? " confirming" : ""),
        text: display.text
      });
      spawnBtn.disabled = display.disabled;
      if (display.noTmux) {
        spawnBtn.title = C.TMUX_UNAVAILABLE_TOOLTIP;
      } else if (live) {
        spawnBtn.title = "Session already running: " + live.name;
      } else if (display.confirming) {
        spawnBtn.title = display.confirmTitle || "";
      }
      spawnBtn.addEventListener("click", function (e) {
        e.stopPropagation();
        C.handleSpawnClick(project.name, task.id);
      });
      footer.appendChild(spawnBtn);
      card.appendChild(footer);

      if (state && (state.status === "error" || state.status === "success")) {
        card.appendChild(renderSpawnStatusLine(state, function (btn) { /* stop bubbling handled below */ }));
      }
    }

    // The Merge button must not depend on main's task status: a spawned
    // agent's own status/AC/notes updates only exist on its task/<id>
    // branch until merged, so main can still say "In Progress" for a
    // task the agent finished. The five safety gates (not this check)
    // are what actually decide whether a merge is safe -- this only
    // decides whether it's worth asking. It's also suppressed while a
    // session is still live (state 2 above takes priority): merging out
    // from under a running agent would just fail gate 1 anyway.
    //
    // Uses the RAW task.hasSpawnBranch here (not effBranch) so a just-
    // succeeded merge/cleanup's confirmation line keeps showing through
    // this same stale-until-refetch window -- only the clickable button
    // itself (justMerged/justCleanedUp below) needs to disappear
    // instantly.
    var hKey = C.spawnKey(project.name, task.id);
    var hState = C.harvestStates[hKey];
    var cState = C.cleanupStates[hKey];
    var justMerged = !!(hState && hState.status === "success");
    var justCleanedUp = !!(cState && cState.status === "success");
    if ((task.hasSpawnBranch && !live) || justMerged || justCleanedUp) {
      if (!justMerged && !justCleanedUp) {
        var hFooter = C.h("div", { className: "card-footer" });
        // task-43: a fully-merged branch (Centrale's own harvest, or
        // entirely out-of-band -- the TASK-1 incident in another repo) means Merge
        // would only ever refuse; offer the cleanup action instead.
        if (task.alreadyMerged && ext) {
          // task-80: merged out-of-band, but the branch is still checked
          // out in a worktree Centrale doesn't manage -- git won't delete
          // it and the server would refuse with 409, so don't offer the
          // click at all; same disabled treatment as the drawer's
          // Resume/Re-spawn (task-70), reason as tooltip.
          hFooter.appendChild(C.externalWorkButton(ext, "btn btn-sm spawn-inline"));
        } else if (task.alreadyMerged) {
          var cDisplay = C.cleanupButtonDisplay(project.name, task.id, cState);
          var cleanupBtn = C.h("button", {
            className: "btn btn-sm spawn-inline" + (cDisplay.confirming ? " confirming" : ""),
            text: cDisplay.text
          });
          cleanupBtn.disabled = cDisplay.disabled;
          cleanupBtn.title = cDisplay.confirming
            ? cDisplay.confirmTitle
            : "This branch is already fully merged -- remove its worktree and delete the branch.";
          cleanupBtn.addEventListener("click", function (e) {
            e.stopPropagation();
            C.handleCleanupClick(project.name, task.id);
          });
          hFooter.appendChild(cleanupBtn);
        } else {
          // task-81: disabled (with the external reason as its title)
          // while the branch is checked out outside Centrale -- see
          // harvestButtonDisplay. No click handler in that case.
          var hDisplay = C.harvestButtonDisplay(hState, task);
          var harvestBtn = C.h("button", { className: "btn btn-sm spawn-inline", text: hDisplay.text });
          harvestBtn.disabled = hDisplay.disabled;
          harvestBtn.title = hDisplay.title;
          if (!hDisplay.external) {
            harvestBtn.addEventListener("click", function (e) {
              e.stopPropagation();
              C.harvestTask(project.name, task.id);
            });
          }
          hFooter.appendChild(harvestBtn);
        }
        card.appendChild(hFooter);
      }

      if (hState && (hState.status === "error" || hState.status === "success" || hState.status === "blocked")) {
        card.appendChild(C.renderHarvestStatusLine(hState));
      }
      if (cState && (cState.status === "error" || cState.status === "success")) {
        card.appendChild(C.renderCleanupStatusLine(cState));
      }
    }

    return card;
  }

  function renderSpawnStatusLine(state) {
    if (state.status === "error") {
      var errLine = C.h("div", { className: "spawn-status-line error", text: state.error });
      errLine.addEventListener("click", function (e) { e.stopPropagation(); });
      return errLine;
    }
    var wrap = C.h("div", { className: "spawn-status-line success" });
    wrap.addEventListener("click", function (e) { e.stopPropagation(); });
    var spawnedText = state.agent ? ("Spawned (" + state.agent + "): " + state.session) : ("Spawned: " + state.session);
    wrap.appendChild(document.createTextNode(spawnedText));
    var row = C.h("div", { className: "attach-row" });
    var codeEl = C.h("code", { text: state.attach });
    var copyBtn = C.h("button", { className: "btn btn-sm", text: "Copy" });
    copyBtn.addEventListener("click", function (e) {
      e.stopPropagation();
      C.copyText(state.attach, copyBtn);
    });
    row.appendChild(codeEl);
    row.appendChild(copyBtn);
    wrap.appendChild(row);
    return wrap;
  }

  // ------------------------------------------------------------------
  // Seam: what the other files reach for through window.Centrale.
  // ------------------------------------------------------------------

  C.dismissTmuxHint = dismissTmuxHint;
  C.isDoneStatus = isDoneStatus;
  C.renderBoard = renderBoard;
  C.renderErrorBanners = renderErrorBanners;
  C.renderProjectChips = renderProjectChips;
  C.renderSpawnStatusLine = renderSpawnStatusLine;
})(window.Centrale = window.Centrale || {});
