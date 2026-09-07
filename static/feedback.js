// static/feedback.js -- clipboard, toasts, and the Backlog.md browser launcher
//
// One of the per-concern frontend files loaded in order by
// static/index.html (task-89); the seam between them is the single
// window.Centrale namespace object, taken here as C. See
// static/state.js for the two rules that seam follows.
(function (C) {
  "use strict";

  // ------------------------------------------------------------------
  // Clipboard
  // ------------------------------------------------------------------

  function copyText(text, btn) {
    var done = function (ok) {
      if (!btn) return;
      var original = btn.getAttribute("data-label") || btn.textContent;
      btn.setAttribute("data-label", original);
      btn.textContent = ok ? "Copied!" : "Copy failed";
      setTimeout(function () { btn.textContent = original; }, 1400);
    };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(function () { done(true); }, function () {
        fallbackCopy(text, done);
      });
    } else {
      fallbackCopy(text, done);
    }
  }

  function fallbackCopy(text, done) {
    try {
      var ta = document.createElement("textarea");
      ta.value = text;
      ta.style.position = "fixed";
      ta.style.opacity = "0";
      ta.style.left = "-9999px";
      document.body.appendChild(ta);
      ta.focus();
      ta.select();
      var ok = document.execCommand("copy");
      document.body.removeChild(ta);
      done(!!ok);
    } catch (e) {
      done(false);
    }
  }

  // ------------------------------------------------------------------
  // Toasts
  // ------------------------------------------------------------------

  // `sticky` drops the 6s auto-dismiss and leaves the toast until the
  // ✕ is clicked. For a notice the user cannot be looking at when it
  // is raised: Open board moves focus to the new tab, so anything said
  // about that board in this one would time out unseen (task-157). The
  // close button is the only way out either way -- there is no separate
  // dismiss path to keep in step.
  function showToast(message, type, sticky) {
    var container = C.byId("toast-container");
    var toast = C.h("div", { className: "toast" + (type ? " " + type : "") });
    toast.appendChild(C.h("span", { className: "toast-msg", text: message }));
    var closeBtn = C.h("button", { className: "toast-close", text: "✕", title: "Dismiss" });
    var remove = function () { if (toast.parentNode) toast.parentNode.removeChild(toast); };
    closeBtn.addEventListener("click", remove);
    toast.appendChild(closeBtn);
    container.appendChild(toast);
    if (!sticky) setTimeout(remove, 6000);
  }

  // ------------------------------------------------------------------
  // Open board (backlog browser) launcher
  // ------------------------------------------------------------------

  var browserLaunchInFlight = {}; // project name -> bool

  // Backlog.md's web UI routes /board/:id -- that path opens the Kanban
  // board with the task's own detail view on top of it. POST /api/browser
  // deliberately stays task-unaware (its job is launching and registering
  // the browser process), so the caller that knows the task appends the
  // path to the base URL the server hands back (task-92). No task id --
  // a project-level open, e.g. a project chip -- means the board root,
  // exactly as before.
  function boardUrlFor(baseUrl, taskId) {
    var base = String(baseUrl).replace(/\/+$/, "");
    if (!taskId) return base;
    return base + "/board/" + encodeURIComponent(taskId);
  }

  // A `backlog browser` keeps the version it was started with, so a
  // board left running across a backlog.md upgrade quietly serves the
  // old one while the terminal reports the new (task-157). The server
  // answers /api/browser with `versionDrift` -- null while the board
  // and the CLI on PATH agree, or while either cannot be asked --
  // and this says so beside the board that was just opened. It computes
  // nothing of its own, and Centrale stops nothing: it launched that
  // process, but the user may be reading it, so the toast names the fix
  // and leaves the doing to them.
  function reportVersionDrift(projectName, drift) {
    if (!drift || typeof drift !== "object" || !drift.running || !drift.cli) return;
    showToast(
      projectName + "'s board is running backlog " + drift.running + ", but the backlog on "
        + "PATH is " + drift.cli + " \u2014 a running board keeps the version it started "
        + "with. Stop that board and open it again to pick up " + drift.cli + ".",
      "warn",
      true
    );
  }

  function openProjectBoard(projectName, btn, taskId) {
    if (browserLaunchInFlight[projectName]) return;
    browserLaunchInFlight[projectName] = true;
    if (btn) btn.disabled = true;

    fetch("/api/browser", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ project: projectName })
    }).then(function (res) {
      return res.json().catch(function () { return {}; }).then(function (data) {
        if (!res.ok) throw new Error((data && data.error) || ("HTTP " + res.status));
        return data;
      });
    }).then(function (data) {
      if (!data || !data.url) throw new Error("malformed /api/browser response");
      window.open(boardUrlFor(data.url, taskId), "_blank", "noopener");
      reportVersionDrift(projectName, data.versionDrift);
    }).catch(function (err) {
      showToast("Failed to open board for " + projectName + ": " + (err.message || err), "error");
    }).then(function () {
      browserLaunchInFlight[projectName] = false;
      if (btn) btn.disabled = false;
    });
  }

  // ------------------------------------------------------------------
  // Seam: what the other files reach for through window.Centrale.
  // ------------------------------------------------------------------

  C.copyText = copyText;
  C.openProjectBoard = openProjectBoard;
  C.showToast = showToast;
})(window.Centrale = window.Centrale || {});
