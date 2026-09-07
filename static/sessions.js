// static/sessions.js -- the sidebar's live-session panel
//
// One of the per-concern frontend files loaded in order by
// static/index.html (task-89); the seam between them is the single
// window.Centrale namespace object, taken here as C. See
// static/state.js for the two rules that seam follows.
(function (C) {
  "use strict";

  // ------------------------------------------------------------------
  // Sessions panel
  // ------------------------------------------------------------------

  // task-50: unlike #board/.column-body (recreated wholesale on every
  // renderBoard(), which is why THAT needs explicit scroll capture/
  // restore), #sessions-list has no overflow of its own -- the sidebar's
  // real scroll container is the persistent #sidebar-nav ancestor
  // (holding both #project-chips and #sessions-list), which this
  // function never clears or recreates, only its descendants' content.
  // Verified empirically via CDP (scroll #sidebar-nav, rebuild
  // #sessions-list's children, re-read scrollTop): it's completely
  // unaffected, so no explicit capture/restore is needed here -- the
  // DOM structure already does the right thing.
  function renderSessionsPanel() {
    C.byId("sessions-count").textContent = String(C.sessionsData.length);
    var list = C.byId("sessions-list");
    C.clearChildren(list);

    if (C.sessionsData.length === 0) {
      var emptyText = C.isTmuxAvailable() ? "No active sessions" : "tmux not available";
      list.appendChild(C.h("div", { className: "sessions-empty", text: emptyText }));
      return;
    }

    C.sessionsData.forEach(function (s) {
      var parsed = C.parseSessionTask(s.name);
      // task-46: the drawer is the single lifecycle surface -- a row
      // whose session name resolves to a real board task (parseable
      // name AND the task is actually in the currently loaded board
      // data, e.g. not excluded by a board fetch) opens that task's
      // drawer on click, exactly like a card. A row that can't be
      // resolved either way stays inert rather than opening a broken
      // drawer -- see findTask's own "not on board" fallback doc.
      var linkedTask = parsed ? C.findTask(parsed.project, parsed.taskId) : null;
      var row = C.h("div", {
        className: "session-row" + (linkedTask ? " session-row-linked" : ""),
        attrs: linkedTask ? { tabindex: "0", role: "button" } : {}
      });
      if (linkedTask) {
        row.addEventListener("click", function () {
          C.openDrawer({ name: parsed.project }, linkedTask);
        });
        row.addEventListener("keydown", function (e) {
          if (e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            C.openDrawer({ name: parsed.project }, linkedTask);
          }
        });
      }
      var top = C.h("div", { className: "top" });
      top.appendChild(C.h("span", { className: "name", text: s.name }));
      // No branchTask at hand here (only the drawer fetches /api/task) --
      // see effectiveAgentState's doc comment -- so this is the raw
      // agentState, never the "likely finished" fallback.
      var displayState = C.effectiveAgentState(s.agentState, s, null);
      top.appendChild(C.renderAgentBadge(displayState));
      if (s.attached) top.appendChild(C.h("span", { className: "live-dot", text: "● attached" }));
      row.appendChild(top);

      if (parsed) {
        var status = C.findTaskStatus(parsed.project, parsed.taskId);
        var descr = parsed.project + " / " + parsed.taskId + (status ? (" — " + status) : "");
        row.appendChild(C.h("div", { className: "created", text: descr }));
      }

      var createdMs = parseInt(s.created, 10);
      if (!isNaN(createdMs)) {
        var d = new Date(createdMs * 1000);
        row.appendChild(C.h("div", { className: "created", text: "started " + d.toLocaleString() }));
      }

      if (Array.isArray(s.files)) {
        var filesLine = s.files.length === 0
          ? "no files touched yet"
          : s.files.join(", ") + (
              (typeof s.filesTotal === "number" && s.filesTotal > s.files.length)
                ? (" +" + (s.filesTotal - s.files.length) + " more")
                : ""
            );
        row.appendChild(C.h("div", { className: "session-files", text: filesLine, title: filesLine }));
      }

      var attachRow = C.h("div", { className: "attach-row" });
      var attachCmd = "tmux attach -t " + s.name;
      attachRow.appendChild(C.h("code", { text: attachCmd }));
      var copyBtn = C.h("button", { className: "btn btn-sm", text: "Copy" });
      copyBtn.addEventListener("click", function (e) {
        e.stopPropagation();
        C.copyText(attachCmd, copyBtn);
      });
      attachRow.appendChild(copyBtn);
      row.appendChild(attachRow);

      // task-120: every row in this panel IS a live session, so every
      // row that Centrale can name a task for gets an End session
      // button, never omitted. task-132: never disabled for its agent
      // state either -- a working agent's button ARMS on the first
      // click and ends on the second (see harvest.js's
      // handleEndSessionClick), and the armed label carries the reason
      // itself, since a row has nowhere to print a line under the
      // button and a tooltip is invisible until hovered. A row whose
      // name doesn't parse is the only genuinely inapplicable case:
      // there is no (project, taskId) to POST, and the attach command
      // above is the honest answer for it.
      if (parsed) {
        var endKey = C.spawnKey(parsed.project, parsed.taskId);
        var endState = C.endSessionStates[endKey];
        var armReason = C.endSessionArmReason(displayState);
        // `true`: End session from a row opens the task's drawer on
        // success (see endSession's own doc) -- the drawer's own
        // End-session button (renderDrawerSessionArea) omits this.
        var endBtn = C.renderEndSessionButton(parsed.project, parsed.taskId,
                                              "btn btn-sm btn-danger-outline", true, armReason);
        endBtn.style.marginTop = "6px";
        row.appendChild(endBtn);
        var endLine = C.renderEndSessionStatusLine(endState);
        if (endLine) row.appendChild(endLine);
      }

      list.appendChild(row);
    });
  }

  // ------------------------------------------------------------------
  // Seam: what the other files reach for through window.Centrale.
  // ------------------------------------------------------------------

  C.renderSessionsPanel = renderSessionsPanel;
})(window.Centrale = window.Centrale || {});
