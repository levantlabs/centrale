"""The whole first-run settings path, walked end to end by the real
frontend against a real server (task-167).

Three settings bugs in a row -- task-158, task-159 and task-167 -- were
all found by a human clicking through a release snapshot, and none by
the 1300-odd passing tests, because every one of those tests drives the
handlers with fabricated payloads and nothing walked the path a stranger
actually takes: boot with no projects.json at all, add the first
project, configure it, and take it back out again. The last of those
bugs was that the taking-it-back-out step was impossible -- Remove was
disabled for the only configured project, in the UI and in the API, so a
first project added with a typo in its path could only be undone by hand
-editing the file the settings UI exists to spare you. This module is
that missing walk.

What is real here: a real git repo with no Backlog.md in it, so the add
exercises the real `backlog init` CLI through settings.py's boundary; a
real projects.json, created from nothing by the first save and read back
off disk at the end; a real HTTP server serving the real routes; and the
real `static/*.js`, loaded under node over the shared DOM shim
(`tests/js_harness.py`) and driven by clicking the actual buttons. The
frontend's assertions are therefore about what a user sees, not about
what a payload contains.

Two deliberate choices, both about the sandbox:

- The server runs **in this process** on an ephemeral port rather than
  as a `server.py` subprocess. A subprocess reads its port from
  projects.json beside it (there is no --port flag, on purpose --
  see build_arg_parser), and TRUE zero config means there is no
  projects.json to put one in, which would leave a real process binding
  the default 7420 -- the port the developer's own Centrale is on. The
  boundaries under test are just as real either way; only the process
  boundary is not.
- This is the one module in this tier that imports the unit tier's
  `tests/js_harness.py`. That shim is the only way this repo runs its
  frontend anywhere, and a second copy of it here would be exactly the
  duplication task-108 removed.

Run explicitly: python3 -m unittest tests_integration.test_settings_integration
"""

from __future__ import annotations

import json
import os
import sys
import threading
import unittest
from unittest import mock

from tests_integration import base

sys.path.insert(0, base.CENTRALE_ROOT)
import server  # noqa: E402

sys.path.insert(0, os.path.join(base.CENTRALE_ROOT, "tests"))
import js_harness  # noqa: E402


def tearDownModule():
    base.assert_test_tmux_footprint_gone()


#: argv: [static dir, base URL, project name, repo path, projects.json path]
FIRST_RUN_DRIVER_JS = js_harness.LOAD_SOURCES_JS + r"""
var BASE = process.argv[2];
var PROJECT = process.argv[3];
var REPO = process.argv[4];
var CONFIG_PATH = process.argv[5];

// projects.json as it stands right now. The file the settings UI exists
// to spare a user from editing by hand is read here at each step of the
// walk, so "it is gone from projects.json" is asserted where it happens
// rather than reconstructed afterwards. null = the file does not exist,
// which is what a first run looks like.
function projectsJson() {
  try {
    return JSON.parse(fs.readFileSync(CONFIG_PATH, "utf8"));
  } catch (e) {
    return null;
  }
}

// Real HTTP, to the real server: the page's own relative URLs, resolved
// against the address it is being served from. Everything else about
// fetch is node's.
var httpFetch = global.fetch;
global.fetch = function (url, opts) { return httpFetch(BASE + url, opts || {}); };

// The confirm-arm disarm timer must not hold node's event loop open for
// five seconds after the last click; nothing here waits on one. waitFor
// below uses REAL_SET_TIMEOUT, held before this line.
var armed = [];
global.setTimeout = function (fn, ms) { armed.push({ fn: fn, ms: ms }); return armed.length; };

var C = loadFrontend(["state.js", "dom.js", "tasks.js", "board.js", "settings.js"]);
wireSettingsShell();

var toasts = [];
C.showToast = function (text, kind) { toasts.push(text); };
C.doRefresh = function () {};          // the board is refetched explicitly below
C.syncDrawerPanePolling = function () {};

function byId(id) { return document.getElementById(id); }

// Real network round trips do not settle in three ticks. Poll the DOM
// for the state the click is supposed to produce, and fail by name.
function waitFor(what, pred) {
  return new Promise(function (resolve, reject) {
    var started = Date.now();
    (function loop() {
      var ready = false;
      try { ready = !!pred(); } catch (e) { ready = false; }
      if (ready) return resolve();
      if (Date.now() - started > 60000) {
        return reject(new Error("timed out waiting for " + what
          + " (settings status: " + byId("settings-status").textContent
          + " | add status: " + byId("settings-add-project-status").textContent + ")"));
      }
      REAL_SET_TIMEOUT(loop, 25);
    })();
  });
}

function projectRows() {
  return byId("settings-projects-list").querySelectorAll(".settings-project-name")
    .map(function (el) { return el.textContent; });
}
function projectPaths() {
  return byId("settings-projects-list").querySelectorAll(".settings-project-path")
    .map(function (el) { return el.textContent; });
}
function removeButtons() {
  return byId("settings-projects-list").querySelectorAll("button").map(function (b) {
    return { text: b.textContent, disabled: b.disabled, title: b.title };
  });
}
function removeButtonNodes() {
  return byId("settings-projects-list").querySelectorAll("button");
}
function checkCommandRows() {
  return byId("settings-check-commands").querySelectorAll("input[data-project]")
    .map(function (input) { return input.getAttribute("data-project"); });
}
function checkCommandValues() {
  var out = {};
  byId("settings-check-commands").querySelectorAll("input[data-project]").forEach(function (input) {
    out[input.getAttribute("data-project")] = input.value;
  });
  return out;
}
function statusText() { return byId("settings-status").textContent; }

// The board, rendered from what the server actually serves -- the
// welcome state is not a claim about a payload, it is which of #board
// and #empty-hint the user is looking at.
function renderBoardFromServer() {
  // ?force=1, exactly as the page's own C.doRefresh(true) does after a
  // settings change: /api/board answers from a ~5s in-memory cache
  // otherwise, and a board fetched a second after an add would still be
  // the one from before it.
  return httpFetch(BASE + "/api/board?force=1").then(function (res) {
    return res.json();
  }).then(function (data) {
    C.boardData = data;
    C.firstLoadDone = true;
    C.renderBoard();
    return {
      projects: (data.projects || []).map(function (p) { return p.name; }),
      emptyHintShown: byId("empty-hint").hidden === false,
      boardShown: byId("board").hidden === false
    };
  });
}

var out = {};

renderBoardFromServer().then(function (board) {
  // 1. A first run: no projects.json on disk at all.
  out.beforeAnything = board;
  out.diskBeforeAnything = projectsJson();

  byId("settings-open").click();
  return waitFor("the settings payload", function () {
    return String(byId("settings-refresh-interval").value || "") !== "";
  });
}).then(function () {
  out.onOpen = { projects: projectRows(), rows: checkCommandRows() };

  // 2. Add the first project. The repo is a real git repo with no
  //    backlog/ in it, so this only succeeds with the checkbox ticked,
  //    and only by really running `backlog init` in it.
  byId("settings-add-project-name").value = PROJECT;
  byId("settings-add-project-path").value = REPO;
  byId("settings-add-project-init").checked = true;
  byId("settings-add-project-btn").click();
  return waitFor("the add to land", function () {
    var text = byId("settings-add-project-status").textContent;
    return text !== "" && text !== "Adding…";
  });
}).then(function () {
  out.afterAdd = {
    addStatus: byId("settings-add-project-status").textContent,
    projects: projectRows(),
    paths: projectPaths(),
    rows: checkCommandRows(),
    values: checkCommandValues(),
    buttons: removeButtons()
  };

  // 3. Configure it: a check command, saved through the main Save.
  byId("settings-check-commands").querySelectorAll("input[data-project]")[0]
    .value = "echo first-run-check";
  byId("settings-save").click();
  return waitFor("the save to land", function () {
    return statusText() === "Saved." || byId("settings-status").className === "error";
  });
}).then(function () {
  out.afterSave = {
    status: statusText(),
    values: checkCommandValues(),
    disk: projectsJson()
  };
  return renderBoardFromServer();
}).then(function (board) {
  out.boardWithProject = board;

  // 4. Remove it -- the step that was impossible until task-167.
  removeButtonNodes()[0].click();          // arm
  out.armedButtons = removeButtons();
  removeButtonNodes()[0].click();          // confirm
  return waitFor("the removal to land", function () {
    return statusText() !== "" && statusText().indexOf("Removing") !== 0;
  });
}).then(function () {
  out.afterRemove = {
    status: statusText(),
    className: byId("settings-status").className,
    projects: projectRows(),
    rows: checkCommandRows(),
    toasts: toasts,
    disk: projectsJson()
  };
  return renderBoardFromServer();
}).then(function (board) {
  // 5. And the board is back to the welcome state it opened on.
  out.afterRemoveBoard = board;
  process.stdout.write(JSON.stringify(out));
}).catch(function (err) {
  process.stderr.write((err && err.stack) || String(err));
  process.exit(1);
});
"""


@base.require_tools("git", "backlog", "node")
class FirstRunSettingsPathIntegrationTests(base.IntegrationCase):
    """Zero config -> add -> configure -> remove -> zero config, clicked
    through the real UI against the real server, with projects.json read
    off disk at each end."""

    PROJECT = "firstproj"

    def setUp(self):
        super().setUp()
        # A real git repo with NO backlog/ in it -- what a stranger
        # points Centrale at, and the reason the add form has an
        # "Initialize Backlog.md in this repo" checkbox at all.
        self.repo_path = os.path.join(self.tmp_dir, self.PROJECT)
        os.makedirs(self.repo_path)
        base.run(["git", "init", "-q", "-b", "main", "."], cwd=self.repo_path)
        base.run(["git", "config", "user.email", "itest@example.com"], cwd=self.repo_path)
        base.run(["git", "config", "user.name", "Centrale Integration Tests"], cwd=self.repo_path)
        with open(os.path.join(self.repo_path, "README.md"), "w", encoding="utf-8") as f:
            f.write("# firstproj\n\nIntegration test fixture repo. Safe to delete.\n")
        base.run(["git", "add", "-A"], cwd=self.repo_path)
        base.run(["git", "commit", "-q", "-m", "initial commit"], cwd=self.repo_path)

        # True zero config: the path is where projects.json WOULD be,
        # and nothing has created it.
        self.config_path = os.path.join(self.tmp_dir, "projects.json")
        self.assertFalse(os.path.exists(self.config_path))
        patcher = mock.patch.object(server, "DEFAULT_CONFIG_PATH", self.config_path)
        patcher.start()
        self.addCleanup(patcher.stop)

        self.config = server.load_config(path=self.config_path)
        self.assertTrue(self.config["zeroConfig"])
        self.assertEqual(self.config["projects"], [])
        self.config["capabilities"] = server.detect_capabilities()
        self.config["version"] = server.detect_version()

        self.httpd = server.CentraleHTTPServer(("127.0.0.1", 0), server.Handler, self.config)
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)
        self.port = self.httpd.server_address[1]
        thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)

    def _disk(self):
        with open(self.config_path, encoding="utf-8") as f:
            return json.load(f)

    @property
    def out(self):
        if not hasattr(self, "_out"):
            self._out = js_harness.run_driver(
                self, FIRST_RUN_DRIVER_JS,
                f"http://127.0.0.1:{self.port}", self.PROJECT, self.repo_path,
                self.config_path, timeout=180)
        return self._out

    # -- the walk ---------------------------------------------------------

    def test_the_whole_first_run_path_ends_where_it_started(self):
        out = self.out

        # A first run: no file, no projects, the welcome panel showing.
        self.assertIsNone(out["diskBeforeAnything"])  # true zero config
        self.assertEqual(out["beforeAnything"]["projects"], [])
        self.assertTrue(out["beforeAnything"]["emptyHintShown"])
        self.assertFalse(out["beforeAnything"]["boardShown"])
        self.assertEqual(out["onOpen"], {"projects": [], "rows": []})

        # The add: a project row AND a check-command row, both from the
        # response the add itself returned (task-158/159 hold up here).
        self.assertEqual(out["afterAdd"]["addStatus"], f'Added "{self.PROJECT}".')
        self.assertEqual(out["afterAdd"]["projects"], [self.PROJECT])
        self.assertEqual(out["afterAdd"]["paths"], [self.repo_path])
        self.assertEqual(out["afterAdd"]["rows"], [self.PROJECT])
        self.assertEqual(out["afterAdd"]["values"], {self.PROJECT: ""})
        # task-167: the ONLY project on the board still has a live
        # Remove button. This is the assertion the bug fails.
        self.assertEqual(len(out["afterAdd"]["buttons"]), 1)
        self.assertFalse(out["afterAdd"]["buttons"][0]["disabled"])
        self.assertNotIn("last project", out["afterAdd"]["buttons"][0]["title"])

        # `backlog init` really ran in the repo, and the project really
        # reached projects.json.
        self.assertTrue(os.path.isfile(os.path.join(self.repo_path, "backlog", "config.yml")))

        # The save: a check command, on disk, under that project.
        self.assertEqual(out["afterSave"]["status"], "Saved.")
        self.assertEqual(out["afterSave"]["values"], {self.PROJECT: "echo first-run-check"})
        entry = next(p for p in out["afterSave"]["disk"]["projects"] if p["name"] == self.PROJECT)
        self.assertEqual(entry["path"], self.repo_path)
        self.assertEqual(entry["checkCommand"], "echo first-run-check")
        self.assertEqual(out["boardWithProject"]["projects"], [self.PROJECT])
        self.assertFalse(out["boardWithProject"]["emptyHintShown"])

        # The removal: armed with a second click, then gone -- from the
        # form, from the board, and from projects.json.
        self.assertEqual(out["armedButtons"][0]["text"], "Confirm remove?")
        self.assertEqual(out["afterRemove"]["status"], f"Removed {self.PROJECT}.")
        self.assertEqual(out["afterRemove"]["className"], "success")
        self.assertEqual(out["afterRemove"]["projects"], [])
        self.assertEqual(out["afterRemove"]["rows"], [])
        # Every toast the whole walk raised, in order -- the add, the
        # save, and the removal that used to be impossible.
        self.assertEqual(out["afterRemove"]["toasts"], [
            f'Project "{self.PROJECT}" added.',
            "Settings saved.",
            f'Project "{self.PROJECT}" removed.',
        ])
        # AC #4's last clause, read off the real file mid-walk and again
        # here: the project is gone from projects.json, and the file is
        # a well-formed config with an empty projects list -- not a
        # deleted or half-written file.
        self.assertEqual(out["afterRemove"]["disk"]["projects"], [])
        self.assertEqual(self._disk()["projects"], [])

        # ...and the board is the welcome state again, exactly as on the
        # first run this walk started from.
        self.assertEqual(out["afterRemoveBoard"]["projects"], [])
        self.assertTrue(out["afterRemoveBoard"]["emptyHintShown"])
        self.assertFalse(out["afterRemoveBoard"]["boardShown"])

        # The repo itself is untouched by the removal: Centrale deletes
        # a config entry, never a directory.
        self.assertTrue(os.path.isdir(os.path.join(self.repo_path, ".git")))
        self.assertTrue(os.path.isfile(os.path.join(self.repo_path, "backlog", "config.yml")))


if __name__ == "__main__":
    unittest.main()
