// static/settings.js -- the settings modal, including the agents editor
//
// One of the per-concern frontend files loaded in order by
// static/index.html (task-89); the seam between them is the single
// window.Centrale namespace object, taken here as C. See
// static/state.js for the two rules that seam follows.
(function (C) {
  "use strict";

  // ------------------------------------------------------------------
  // Settings modal
  // ------------------------------------------------------------------

  C.settingsOpen = false;
  var settingsSaveInFlight = false;
  var lastSettingsData = null;
  var settingsRemovePending = {}; // project name -> {expires: epochMs}
  var settingsProjectActionInFlight = false; // guards add/remove while a request is in flight
  // task-78: the Agents editor's rows -- [{name, cmd (shell-quoted text),
  // promptSuffix, builtin, onPath}], the working copy the inputs write
  // into so a re-render never loses typing. `settingsAgentsDirty` gates
  // whether Save posts an `agents` field at all: an untouched section
  // never rewrites the map (a config running on the built-in defaults
  // keeps running on them).
  var settingsAgentRows = [];
  var settingsAgentsDirty = false;
  var settingsAgentRemovePending = {}; // row index -> {expires: epochMs}
  var BUILTIN_DEFAULT_AGENT = "claude"; // server.DEFAULT_AGENT_NAME: what a missing default falls back to

  function openSettingsModal() {
    C.settingsOpen = true;
    C.byId("settings-backdrop").classList.add("open");
    C.byId("settings-modal").classList.add("open");
    C.byId("settings-modal").setAttribute("aria-hidden", "false");
    setSettingsStatus("", null);
    clearSettingsFieldErrors();
    // task-78: the Agents section starts collapsed on every open.
    C.byId("settings-agents-section").open = false;

    fetch("/api/settings").then(function (res) {
      return res.json().catch(function () { return null; }).then(function (data) {
        if (!res.ok) throw new Error((data && data.error) || "Failed to load settings");
        return data;
      });
    }).then(function (data) {
      populateSettingsForm(data);
    }).catch(function (err) {
      setSettingsStatus("Failed to load settings: " + (err.message || err), "error");
    });
  }

  function closeSettingsModal() {
    C.settingsOpen = false;
    C.byId("settings-backdrop").classList.remove("open");
    C.byId("settings-modal").classList.remove("open");
    C.byId("settings-modal").setAttribute("aria-hidden", "true");
  }

  function setSettingsStatus(text, kind) {
    var el = C.byId("settings-status");
    el.textContent = text;
    el.className = kind ? kind : "";
  }

  function clearSettingsFieldErrors() {
    var errs = document.querySelectorAll("#settings-modal .settings-field-error");
    errs.forEach(function (el) { el.hidden = true; el.textContent = ""; });
  }

  // task-60/61: two toggles, one tiered knob. Preview off -> "off" (the
  // reply toggle is moot and shown disabled); preview on -> "interact"
  // when replying is allowed, else "view".
  function settingsSessionPreviewModeFromToggles() {
    if (!C.byId("settings-session-preview-toggle").checked) return "off";
    return C.byId("settings-session-reply-toggle").checked ? "interact" : "view";
  }

  function syncSettingsReplyToggle() {
    var previewOn = C.byId("settings-session-preview-toggle").checked;
    var reply = C.byId("settings-session-reply-toggle");
    reply.disabled = !previewOn;
    C.byId("settings-session-reply-field").classList.toggle("disabled", !previewOn);
  }
  C.byId("settings-session-preview-toggle").addEventListener("change", syncSettingsReplyToggle);

  function populateSettingsForm(data) {
    lastSettingsData = data;
    C.byId("settings-harvest-mode-toggle").checked = data.harvestMode === "auto";
    C.byId("settings-session-preview-toggle").checked = data.sessionPreviewMode !== "off";
    C.byId("settings-session-reply-toggle").checked = data.sessionPreviewMode === "interact";
    syncSettingsReplyToggle();
    C.byId("settings-refresh-interval").value = data.refreshIntervalSeconds;

    var container = C.byId("settings-check-commands");
    C.clearChildren(container);
    var checkCommands = data.checkCommands || {};
    // task-158: the names come from THIS payload, never from the board's
    // C.knownProjects. That list is "every project name ever seen" and
    // is only ever pushed to, so a removed project stayed in it and its
    // row outlived the project -- the next Save then sent
    // checkCommands.<removed>, which the server rightly refuses as an
    // unknown project, on a form showing no bad field. The payload
    // already carries the names and their order, so this render needs
    // no fetch and waits on no board refresh.
    (data.projects || []).forEach(function (project) {
      var name = project.name;
      var row = C.h("div", { className: "settings-check-command-row" });
      row.appendChild(C.h("span", { className: "settings-check-command-name", text: name, title: name }));
      var input = document.createElement("input");
      input.type = "text";
      input.className = "settings-input";
      input.setAttribute("data-project", name);
      input.placeholder = "e.g. python3 -m unittest discover tests";
      input.value = checkCommands[name] || "";
      row.appendChild(input);
      var err = C.h("div", { className: "settings-field-error" });
      err.hidden = true;
      err.setAttribute("data-project-error", name);
      row.appendChild(err);
      container.appendChild(row);
    });

    settingsAgentRows = (data.agentEntries || []).map(function (e) {
      return {
        name: e.name,
        cmd: typeof e.cmdText === "string" ? e.cmdText : (e.cmd || []).join(" "),
        promptSuffix: e.promptSuffix || "",
        builtin: !!e.builtin,
        onPath: e.onPath !== false
      };
    });
    settingsAgentsDirty = false;
    settingsAgentRemovePending = {};
    renderAgentsTable();
    syncSettingsAgentPicker(data.defaultAgent);

    renderProjectsList(data.projects || []);
  }

  // task-78: Agents editor ------------------------------------------

  function settingsAgentPickerFallback(removedName) {
    // What spawns will use once `removedName` is gone: the picker's
    // current choice if that isn't the one being removed, else the
    // built-in default -- exactly spawn.resolve_agent's own fallback.
    var current = C.byId("settings-default-agent").value;
    return (current && current !== removedName) ? current : BUILTIN_DEFAULT_AGENT;
  }

  // Board tasks whose first assignee is this agent (case-insensitive,
  // like the server's own matching) -- read from the board already on
  // screen, no extra request.
  function tasksAssignedToAgent(name) {
    var hits = [];
    var wanted = String(name || "").toLowerCase();
    if (!wanted) return hits;
    (C.boardData.projects || []).forEach(function (project) {
      (project.tasks || []).forEach(function (task) {
        var assignee = C.firstAssignee(task);
        if (assignee && assignee.toLowerCase() === wanted) hits.push(project.name + " " + task.id);
      });
    });
    return hits;
  }

  function updateSettingsAgentsSummary() {
    var summary = C.byId("settings-agents-summary");
    var count = settingsAgentRows.length;
    C.clearChildren(summary);
    // One inline child holding all the text: the summary is a flex row
    // (caret + text), and a single span keeps its text one run.
    var label = C.h("span");
    label.appendChild(document.createTextNode("Agents (" + count + " configured"));
    var current = C.byId("settings-default-agent").value;
    if (current) {
      label.appendChild(C.h("span", { className: "settings-agents-summary-detail", text: ", default: " + current }));
    }
    label.appendChild(document.createTextNode(")"));
    summary.appendChild(label);
  }

  // The default-agent <select> lists the editor's current row names, so
  // a just-added agent is pickable before Save and a removed one
  // disappears. Keeps the current choice when it still exists, else
  // `preferred` (the server's value on load), else the built-in default,
  // else the first row.
  function syncSettingsAgentPicker(preferred) {
    var select = C.byId("settings-default-agent");
    var current = select.value;
    var names = [];
    settingsAgentRows.forEach(function (row) {
      var name = String(row.name || "").trim();
      if (name && names.indexOf(name) === -1) names.push(name);
    });
    C.clearChildren(select);
    names.forEach(function (name) {
      var opt = document.createElement("option");
      opt.value = name;
      opt.textContent = name;
      select.appendChild(opt);
    });
    var pick = null;
    [preferred, current, BUILTIN_DEFAULT_AGENT].forEach(function (candidate) {
      if (pick === null && candidate && names.indexOf(candidate) !== -1) pick = candidate;
    });
    if (pick === null && names.length) pick = names[0];
    if (pick !== null) select.value = pick;
    updateSettingsAgentsSummary();
  }

  function markSettingsAgentsDirty() {
    settingsAgentsDirty = true;
  }

  function renderAgentsTable() {
    var container = C.byId("settings-agents-table");
    C.clearChildren(container);
    settingsAgentRows.forEach(function (row, index) {
      var el = C.h("div", { className: "settings-agent-row" + (row.builtin ? " builtin" : "") });
      el.setAttribute("data-agent-index", String(index));

      var head = C.h("div", { className: "settings-agent-head" });
      var nameInput = document.createElement("input");
      nameInput.type = "text";
      nameInput.className = "settings-input settings-agent-name";
      nameInput.placeholder = "name (matched against @assignee)";
      nameInput.value = row.name;
      nameInput.setAttribute("data-agent-field", "name");
      nameInput.setAttribute("aria-label", "Agent name");
      if (row.builtin) {
        nameInput.readOnly = true;
        nameInput.title = "Built-in agent -- its command and suffix can be overridden, the name cannot change.";
      }
      nameInput.addEventListener("input", function () {
        row.name = nameInput.value;
        markSettingsAgentsDirty();
      });
      nameInput.addEventListener("change", function () { syncSettingsAgentPicker(null); });
      head.appendChild(nameInput);

      if (row.builtin) {
        head.appendChild(C.h("span", { className: "settings-agent-tag", text: "built-in", title: "Ships with Centrale; cannot be removed." }));
      }
      if (!row.onPath) {
        head.appendChild(C.h("span", {
          className: "settings-agent-tag warn",
          text: "not on PATH",
          title: "The command's executable was not found on PATH when the settings were last loaded or saved. Spawning with it will fail until it is installed or the command is fixed."
        }));
      }
      if (!row.builtin) {
        var pending = settingsAgentRemovePending[index];
        var armed = !!(pending && pending.expires > Date.now());
        var removeBtn = C.h("button", {
          className: "btn btn-sm btn-danger-outline" + (armed ? " confirming" : ""),
          text: armed ? "Confirm remove?" : "Remove",
          title: "Removes this agent from the map on the next Save."
        });
        removeBtn.setAttribute("data-agent-remove", String(index));
        removeBtn.addEventListener("click", function () { handleRemoveAgentClick(index); });
        head.appendChild(removeBtn);
      }
      el.appendChild(head);

      var cmdInput = document.createElement("input");
      cmdInput.type = "text";
      cmdInput.className = "settings-input settings-agent-cmd";
      cmdInput.placeholder = "command, e.g. claude --model opus  (shell quoting: 'one arg with spaces')";
      cmdInput.value = row.cmd;
      cmdInput.setAttribute("data-agent-field", "cmd");
      cmdInput.setAttribute("aria-label", "Agent command");
      cmdInput.title = "The argv to launch, written like a shell command line. Quote an argument that contains spaces; it round-trips to the exact argv list in projects.json.";
      cmdInput.addEventListener("input", function () {
        row.cmd = cmdInput.value;
        markSettingsAgentsDirty();
      });
      el.appendChild(cmdInput);
      el.appendChild(settingsAgentFieldError(index, "cmd"));

      var suffixInput = document.createElement("input");
      suffixInput.type = "text";
      suffixInput.className = "settings-input settings-agent-suffix";
      suffixInput.placeholder = "prompt suffix (optional) -- appended to the standard spawn prompt";
      suffixInput.value = row.promptSuffix;
      suffixInput.setAttribute("data-agent-field", "promptSuffix");
      suffixInput.setAttribute("aria-label", "Agent prompt suffix");
      suffixInput.addEventListener("input", function () {
        row.promptSuffix = suffixInput.value;
        markSettingsAgentsDirty();
      });
      el.appendChild(suffixInput);
      el.appendChild(settingsAgentFieldError(index, "promptSuffix"));
      el.appendChild(settingsAgentFieldError(index, "name"));

      var warning = C.h("div", { className: "settings-agent-warning" });
      warning.setAttribute("data-agent-warning", String(index));
      warning.hidden = !row.removeWarning;
      warning.textContent = row.removeWarning || "";
      el.appendChild(warning);

      container.appendChild(el);
    });
  }

  function settingsAgentFieldError(index, field) {
    var err = C.h("div", { className: "settings-field-error" });
    err.hidden = true;
    err.setAttribute("data-agent-error", index + "." + field);
    return err;
  }

  function handleAddAgentClick() {
    settingsAgentRows.push({ name: "", cmd: "", promptSuffix: "", builtin: false, onPath: true });
    markSettingsAgentsDirty();
    renderAgentsTable();
    updateSettingsAgentsSummary();
    var rows = document.querySelectorAll("#settings-agents-table .settings-agent-row");
    var last = rows[rows.length - 1];
    var nameInput = last && last.querySelector('[data-agent-field="name"]');
    if (nameInput) nameInput.focus();
  }

  // Removing a user agent: an immediate local edit (it leaves the map on
  // the next Save) -- unless something on the board still points at it,
  // or it is the picked default, in which case the first click only
  // arms the button and explains what will happen (which tasks lose
  // their agent and what they fall back to); a second click within the
  // usual 5s window removes it. Never a silent removal.
  function handleRemoveAgentClick(index) {
    var row = settingsAgentRows[index];
    if (!row || row.builtin) return;
    var pending = settingsAgentRemovePending[index];
    if (pending && pending.expires > Date.now()) {
      delete settingsAgentRemovePending[index];
      removeAgentRow(index);
      return;
    }

    var name = String(row.name || "").trim();
    var reasons = [];
    var assigned = tasksAssignedToAgent(name);
    var fallback = settingsAgentPickerFallback(name);
    if (assigned.length) {
      var shown = assigned.slice(0, 5).join(", ") + (assigned.length > 5 ? ", …" : "");
      reasons.push(
        assigned.length + (assigned.length === 1 ? " task is" : " tasks are") + " assigned to @" + name +
        " (" + shown + "). Once it's removed, spawning them falls back to the default agent (" + fallback + ")."
      );
    }
    if (name && C.byId("settings-default-agent").value === name) {
      reasons.push("@" + name + " is the current default agent; the default will change to " + fallback + ".");
    }
    if (!reasons.length) {
      removeAgentRow(index);
      return;
    }

    row.removeWarning = reasons.join(" ") + " Click Remove again to confirm.";
    settingsAgentRemovePending[index] = { expires: Date.now() + C.SPAWN_CONFIRM_WINDOW_MS };
    renderAgentsTable();
    setTimeout(function () {
      var current = settingsAgentRemovePending[index];
      if (current && current.expires <= Date.now()) {
        delete settingsAgentRemovePending[index];
        if (settingsAgentRows[index] === row) delete row.removeWarning;
        renderAgentsTable();
      }
    }, C.SPAWN_CONFIRM_WINDOW_MS + 100);
  }

  function removeAgentRow(index) {
    settingsAgentRows.splice(index, 1);
    settingsAgentRemovePending = {};
    markSettingsAgentsDirty();
    renderAgentsTable();
    syncSettingsAgentPicker(null);
  }

  function settingsAgentsPayload() {
    return settingsAgentRows.map(function (row) {
      return { name: row.name, cmd: row.cmd, promptSuffix: row.promptSuffix };
    });
  }

  // Renders the Projects list (name + path + a confirm-armed Remove
  // button) from the last-known /api/settings payload. Called both after
  // a fresh fetch and, with no network round-trip, whenever a remove's
  // arm/disarm state or in-flight state changes -- same "instant local
  // re-render, no refetch needed" pattern as the card/drawer spawn
  // buttons use for their own confirm-arm state.
  function renderProjectsList(projects) {
    var container = C.byId("settings-projects-list");
    C.clearChildren(container);
    projects.forEach(function (p) {
      var row = C.h("div", { className: "settings-project-row" });
      var info = C.h("div", { className: "settings-project-info" });
      info.appendChild(C.h("div", { className: "settings-project-name", text: p.name, title: p.name }));
      info.appendChild(C.h("div", { className: "settings-project-path", text: p.path, title: p.path }));
      row.appendChild(info);

      var pending = settingsRemovePending[p.name];
      var armed = !!(pending && pending.expires > Date.now());
      var btn = C.h("button", {
        className: "btn btn-sm btn-danger-outline" + (armed ? " confirming" : ""),
        text: armed ? "Confirm remove?" : "Remove"
      });
      // task-167: the ONLY thing that disables Remove here is a request
      // already in flight. It used to also refuse the last remaining
      // project, which left a first-time user who typed the wrong path
      // for their first project with no way out of the UI at all -- and
      // a board with zero projects has been a supported state, welcome
      // panel and all, since task-53. Whether a removal would strand a
      // live session or an unmerged branch is a question only the
      // server can answer, and only at the moment of the click, so it
      // is asked there and its refusal is rendered below rather than
      // guessed at here from a payload that may already be stale.
      btn.disabled = settingsProjectActionInFlight;
      btn.title = "Removes this project from the dashboard's config only -- the repo itself is never touched.";
      btn.addEventListener("click", function () { handleRemoveProjectClick(p.name); });
      row.appendChild(btn);

      container.appendChild(row);
    });
  }

  function handleRemoveProjectClick(name) {
    if (settingsProjectActionInFlight) return;
    var pending = settingsRemovePending[name];
    if (pending && pending.expires > Date.now()) {
      delete settingsRemovePending[name];
      removeProject(name);
      return;
    }

    settingsRemovePending[name] = { expires: Date.now() + C.SPAWN_CONFIRM_WINDOW_MS };
    renderProjectsList((lastSettingsData && lastSettingsData.projects) || []);
    setTimeout(function () {
      var current = settingsRemovePending[name];
      if (current && current.expires <= Date.now()) {
        delete settingsRemovePending[name];
        renderProjectsList((lastSettingsData && lastSettingsData.projects) || []);
      }
    }, C.SPAWN_CONFIRM_WINDOW_MS + 100);
  }

  function removeProject(name) {
    settingsProjectActionInFlight = true;
    renderProjectsList((lastSettingsData && lastSettingsData.projects) || []);
    setSettingsStatus("Removing " + name + "…", null);

    fetch("/api/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ removeProject: name })
    }).then(function (res) {
      return res.json().catch(function () { return {}; }).then(function (data) {
        if (!res.ok) {
          var err = new Error((data && data.error) || ("HTTP " + res.status));
          err.fields = data && data.fields;
          throw err;
        }
        return data;
      });
    }).then(function (data) {
      populateSettingsForm(data);
      setSettingsStatus("Removed " + name + ".", "success");
      C.showToast("Project \"" + name + "\" removed.", "success");
      C.doRefresh(true); // so the removed project's cards disappear immediately
    }).catch(function (err) {
      // The refusal this most often carries is task-167's strand guard,
      // whose whole value is the sentence naming the session or branch
      // in the way. There is no form field to hang it on, so show the
      // reason itself rather than the "invalid settings: removeProject:
      // ..." envelope wrapped around it.
      var reason = (err.fields && err.fields.removeProject) || err.message || String(err);
      setSettingsStatus("Could not remove " + name + ": " + reason, "error");
    }).then(function () {
      settingsProjectActionInFlight = false;
      renderProjectsList((lastSettingsData && lastSettingsData.projects) || []);
    });
  }

  function handleAddProjectClick() {
    if (settingsProjectActionInFlight) return;
    clearSettingsFieldErrors();

    var name = C.byId("settings-add-project-name").value;
    var path = C.byId("settings-add-project-path").value;
    var initBacklog = C.byId("settings-add-project-init").checked;

    settingsProjectActionInFlight = true;
    var btn = C.byId("settings-add-project-btn");
    btn.disabled = true;
    var statusEl = C.byId("settings-add-project-status");
    statusEl.textContent = "Adding…";
    statusEl.className = "settings-hint";

    fetch("/api/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ addProject: { name: name, path: path, initBacklog: initBacklog } })
    }).then(function (res) {
      return res.json().catch(function () { return {}; }).then(function (data) {
        if (!res.ok) {
          var err = new Error((data && data.error) || ("HTTP " + res.status));
          err.fields = data && data.fields;
          throw err;
        }
        return data;
      });
    }).then(function (data) {
      populateSettingsForm(data);
      C.byId("settings-add-project-name").value = "";
      C.byId("settings-add-project-path").value = "";
      C.byId("settings-add-project-init").checked = false;
      statusEl.textContent = "Added \"" + name + "\".";
      statusEl.className = "settings-hint success";
      C.showToast("Project \"" + name + "\" added.", "success");
      C.doRefresh(true); // so the new project's board column appears immediately
    }).catch(function (err) {
      var summary = err.fields ? applySettingsFieldErrors(err.fields) : "";
      statusEl.textContent = summary || err.message || String(err);
      statusEl.className = "settings-hint error";
    }).then(function () {
      settingsProjectActionInFlight = false;
      btn.disabled = false;
      // task-167: the Projects list was last rendered by
      // populateSettingsForm ABOVE, while this flag was still true --
      // so every Remove button in it, including the one belonging to
      // the project just added, was rendered disabled and nothing ever
      // re-enabled it. Adding a project and then immediately removing
      // it is the first-run path, and it was dead until the modal was
      // closed and reopened. Re-render once the flag is down, the same
      // way a removal's own final block does.
      renderProjectsList((lastSettingsData && lastSettingsData.projects) || []);
    });
  }

  // Renders one server field error next to the field it names, and
  // returns whether it found anywhere to put it (task-158) -- a caller
  // has to know, because "Fix the highlighted field" highlighting
  // nothing is indistinguishable from a bug.
  function showSettingsFieldError(fieldName, message) {
    var el = C.byId("settings-error-" + fieldName);
    if (el) {
      el.hidden = false;
      el.textContent = message;
      if (fieldName === "defaultAgent") C.byId("settings-agents-section").open = true;
      return true;
    }
    // task-78: "agents.<index>.<field>" -- under that row's own input;
    // "agents.<name>" / "agents" (map-level) -- under the table.
    if (fieldName.indexOf("agents") === 0) {
      var rest = fieldName.slice("agents.".length);
      var rowErr = document.querySelector('#settings-agents-table [data-agent-error="' + rest + '"]');
      if (rowErr && fieldName.length > "agents.".length) {
        rowErr.hidden = false;
        rowErr.textContent = message;
      } else {
        var tableErr = C.byId("settings-error-agents");
        tableErr.hidden = false;
        tableErr.textContent = (tableErr.textContent ? tableErr.textContent + " " : "") + message;
      }
      C.byId("settings-agents-section").open = true;
      return true;
    }
    // "checkCommands.<project>" -- shown under that project's own row,
    // when there is one: the server validates against projects.json, so
    // it can name a project this form is not showing.
    if (fieldName.indexOf("checkCommands.") === 0) {
      var project = fieldName.slice("checkCommands.".length);
      var rowErr = document.querySelector('#settings-check-commands [data-project-error="' + project + '"]');
      if (rowErr) {
        rowErr.hidden = false;
        rowErr.textContent = message;
        return true;
      }
    }
    return false;
  }

  // task-158: show every field error the response carried, and return
  // the one-line status to go with them. A field whose row is on screen
  // is highlighted and counted; one with nowhere to render is spelled
  // out instead, reason and all, rather than pointing at a highlight
  // that does not exist. Shared by Save and Add project, which used to
  // write the same summary string twice.
  function applySettingsFieldErrors(fields) {
    var shown = [];
    var missing = [];
    Object.keys(fields).forEach(function (name) {
      (showSettingsFieldError(name, fields[name]) ? shown : missing).push(name);
    });
    var parts = [];
    if (shown.length) {
      parts.push("Fix the highlighted field" + (shown.length === 1 ? "" : "s") + " and try again.");
    }
    if (missing.length) {
      parts.push(
        "The server refused " +
        missing.map(function (name) { return name + " (" + fields[name] + ")"; }).join(", ") +
        " -- " + (missing.length === 1 ? "that field is" : "those fields are") +
        " not on this form, so reload the page and try again.");
    }
    return parts.join(" ");
  }

  function saveSettings() {
    if (settingsSaveInFlight) return;
    settingsSaveInFlight = true;
    clearSettingsFieldErrors();
    setSettingsStatus("Saving…", null);
    var saveBtn = C.byId("settings-save");
    saveBtn.disabled = true;

    var checkCommands = {};
    document.querySelectorAll("#settings-check-commands input[data-project]").forEach(function (input) {
      checkCommands[input.getAttribute("data-project")] = input.value;
    });

    var body = {
      harvestMode: C.byId("settings-harvest-mode-toggle").checked ? "auto" : "click",
      sessionPreviewMode: settingsSessionPreviewModeFromToggles(),
      refreshIntervalSeconds: parseInt(C.byId("settings-refresh-interval").value, 10),
      checkCommands: checkCommands,
      defaultAgent: C.byId("settings-default-agent").value
    };
    // task-78: only a touched Agents section rewrites the map -- an
    // untouched one leaves projects.json's "agents" key exactly as is
    // (present or absent).
    if (settingsAgentsDirty) body.agents = settingsAgentsPayload();

    fetch("/api/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body)
    }).then(function (res) {
      return res.json().catch(function () { return {}; }).then(function (data) {
        if (!res.ok) {
          var err = new Error((data && data.error) || ("HTTP " + res.status));
          err.fields = data && data.fields;
          throw err;
        }
        return data;
      });
    }).then(function (data) {
      populateSettingsForm(data);
      var warnings = Array.isArray(data.warnings) ? data.warnings : [];
      if (warnings.length) {
        // task-78: saved, but an agent's executable isn't on PATH -- a
        // typo caught before a spawn wastes it. Warn, never block.
        setSettingsStatus("Saved. Warning: " + warnings.join(" "), "warning");
        C.byId("settings-agents-section").open = true;
      } else {
        setSettingsStatus("Saved.", "success");
      }
      C.showToast("Settings saved.", "success");
      // task-60: apply the live-pane mode from the response right away
      // (the board refetch below would too, but only once it lands) so
      // a drawer opened in that window never starts a poll the server
      // is about to refuse -- and an open drawer's pane stops/starts
      // the instant the toggle is saved.
      if (typeof data.sessionPreviewMode === "string") {
        C.sessionPreviewMode = data.sessionPreviewMode;
        C.syncDrawerPanePolling();
      }
      C.doRefresh(true); // harvestMode/refreshIntervalSeconds/sessionPreviewMode may have changed
    }).catch(function (err) {
      var summary = err.fields ? applySettingsFieldErrors(err.fields) : "";
      setSettingsStatus(summary || err.message || String(err), "error");
    }).then(function () {
      settingsSaveInFlight = false;
      saveBtn.disabled = false;
    });
  }

  C.byId("settings-open").addEventListener("click", openSettingsModal);
  // task-53: the first-run empty state's "Add a project" button opens
  // the exact same settings modal the gear icon does -- no separate
  // flow, just a more discoverable entry point for a board with nothing
  // configured yet.
  C.byId("empty-hint-add-project").addEventListener("click", openSettingsModal);
  C.byId("settings-close").addEventListener("click", closeSettingsModal);
  C.byId("settings-backdrop").addEventListener("click", closeSettingsModal);
  C.byId("settings-save").addEventListener("click", saveSettings);
  C.byId("settings-add-project-btn").addEventListener("click", handleAddProjectClick);
  C.byId("settings-add-agent-btn").addEventListener("click", handleAddAgentClick);
  C.byId("settings-default-agent").addEventListener("change", updateSettingsAgentsSummary);

  // ------------------------------------------------------------------
  // Seam: what the other files reach for through window.Centrale.
  // ------------------------------------------------------------------

  C.closeSettingsModal = closeSettingsModal;
})(window.Centrale = window.Centrale || {});
