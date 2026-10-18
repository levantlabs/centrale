"""Per-task placement in the UI (doc-3 revision 5, task-225): the machine
picker in the spawn control, a project's machines in Settings, and a
conflicted location on the card, the drawer and the sessions list.

Behavioural: the real static/*.js sources under node over tests/js_harness.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import js_harness  # noqa: E402

SPAWN_DRIVER_JS = js_harness.LOAD_SOURCES_JS + r"""
var C = loadFrontend(["state.js", "dom.js", "tasks.js", "board.js", "spawn.js", "harvest.js", "drawer.js", "sessions.js"]);
var realSessionsPanel = C.renderSessionsPanel;
C.renderSessionsPanel = function () {};
C.syncDrawerPanePolling = function () {};
C.renderProjectChips = function () {};
C.renderErrorBanners = function () {};
C.doRefresh = function () {};
C.fetchSessions = function () {};
C.showToast = function () {};

var bodies = [];
global.fetch = function (url, opts) {
  bodies.push(opts && opts.body ? JSON.parse(opts.body) : null);
  return Promise.resolve({ ok: true, status: 200, json: function () { return Promise.resolve({ session: "s", attach: "a", agent: "claude" }); } });
};

function task(id, extra) {
  var t = { id: id, title: "A task", status: "To Do", ready: true, assignees: ["@claude"],
            hasSpawnBranch: false, alreadyMerged: false, worktreeDirty: false, branchCheckout: null, lastDiscardedAt: null };
  Object.keys(extra || {}).forEach(function (k) { t[k] = extra[k]; });
  return t;
}
var PROJECTS = {
  plain: { name: "plain", tasks: [task("TASK-1")], statuses: ["To Do", "Done"] },
  multi: { name: "multi", machines: ["box-b", "box-c"], defaultMachine: "box-b", statuses: ["To Do", "Done"], tasks: [
    task("TASK-1", { location: { machine: null, conflict: null, reach: null } }),
    task("TASK-2", { location: { machine: "box-c", conflict: null, reach: "reachable" }, machine: "box-c", worktree: "~/w/multi-task-2", hasSpawnBranch: true }),
    task("TASK-3", { location: { machine: null, conflict: ["hub", "box-b"], reach: null }, hasSpawnBranch: true }),
    task("TASK-4", { location: { machine: null, conflict: null, reach: null } })
  ] },
  hubdef: { name: "hubdef", machines: ["box-b"], defaultMachine: "hub", statuses: ["To Do", "Done"],
    tasks: [task("TASK-1", { location: { machine: null, conflict: null, reach: null } })] }
};
C.boardData = { projects: [PROJECTS.plain, PROJECTS.multi, PROJECTS.hubdef], capabilities: { tmux: true },
  machines: [{ name: "box-b", reach: "reachable" }, { name: "box-c", reach: "reachable" }],
  spawnAgents: { names: ["claude"], defaultAgent: "claude", required: false } };
C.knownProjects.length = 0;
["plain", "multi", "hubdef"].forEach(function (n) { C.knownProjects.push(n); });
C.activeProjects = new Set(["plain", "multi", "hubdef"]);
C.sessionsData = [];
C.firstLoadDone = true;
C.renderBoard();

function cardOf(project, id) {
  var found = null;
  (function walk(n) {
    if (String(n.className || "").split(/\s+/).indexOf("card") !== -1) {
      var hit = n.childNodes.filter(function (c) { return String(c.className || "").indexOf("card-id") !== -1 && c.textContent === id; })[0];
      var chip = n.querySelectorAll(".proj-chip")[0];
      if (hit && chip && chip.textContent === project) found = n;
      return;
    }
    n.childNodes.forEach(walk);
  })(C.byId("board"));
  return found;
}
function read(project, id) {
  var card = cardOf(project, id);
  if (!card) return null;
  var picker = card.querySelectorAll(".spawn-machine-picker")[0] || null;
  var btn = card.querySelectorAll(".spawn-inline")[0] || null;
  return {
    picker: picker ? picker.childNodes.map(function (o) { return o.textContent; }) : null,
    selected: picker ? picker.value : null,
    button: btn ? { text: btn.textContent, disabled: !!btn.disabled, title: btn.title || "" } : null,
    chips: card.querySelectorAll(".machine-chip").map(function (c) { return { text: c.textContent, title: c.title, className: c.className }; }),
    select: picker, btn: btn
  };
}
function plain(r) { return r && { picker: r.picker, selected: r.selected, button: r.button, chips: r.chips }; }

var out = {};
out.noMachines = plain(read("plain", "TASK-1"));
out.fresh = plain(read("multi", "TASK-1"));
out.hubDefault = plain(read("hubdef", "TASK-1"));
out.onWorker = plain(read("multi", "TASK-2"));
out.conflict = plain(read("multi", "TASK-3"));

// A pick is sent as the machine; so is the preselected default.
var pick = read("multi", "TASK-1");
pick.select.value = "box-c";
pick.select.dispatch("change", {});
out.afterPick = { selected: read("multi", "TASK-1").selected };
read("multi", "TASK-1").btn.click();
out.pickBody = bodies.pop();
read("multi", "TASK-4").btn.click();
out.defaultBody = bodies.pop();
read("hubdef", "TASK-1").btn.click();
out.hubBody = bodies.pop();
read("plain", "TASK-1").btn.click();
out.plainBody = bodies.pop();

// The drawer offers the same picker, and shows a conflict's reason.
C.currentDrawer = { project: "multi", id: "TASK-4", summary: C.findTask("multi", "TASK-4") };
C.renderDrawerSpawnArea();
var area = C.byId("drawer-spawn-area");
out.drawer = { picker: (area.querySelectorAll(".spawn-machine-picker")[0] || { childNodes: [] }).childNodes.map(function (o) { return o.textContent; }) };
C.currentDrawer = { project: "multi", id: "TASK-3", summary: C.findTask("multi", "TASK-3") };
C.renderDrawerSpawnArea();
area = C.byId("drawer-spawn-area");
C.renderDrawerHarvestArea();
var harvest = C.byId("drawer-harvest-area");
out.drawerConflict = { picker: area.querySelectorAll(".spawn-machine-picker").length,
  buttons: harvest.querySelectorAll("button").map(function (b) { return { disabled: !!b.disabled, title: b.title }; }) };
out.blockReason = { conflict: C.machineBlockReason("multi", "TASK-3"), worker: C.machineBlockReason("multi", "TASK-2"), none: C.machineBlockReason("plain", "TASK-1") };

// A live session on the conflicted task says so in the sessions list.
C.sessionsData = [{ name: "centrale-multi-task-3", created: "100", attached: false },
                  { name: "centrale-multi-task-2", created: "100", attached: false, machine: "box-c", worktree: "~/w/multi-task-2" }];
realSessionsPanel();
out.sessions = C.byId("sessions-list").querySelectorAll(".session-machine").map(function (n) { return { text: n.textContent, title: n.title }; });
setTimeout(function () { process.stdout.write(JSON.stringify(out)); }, 0);
"""


@js_harness.requires_node
class SpawnMachinePickerTests(unittest.TestCase):
    @property
    def out(self):
        return js_harness.cached_driver(self, SPAWN_DRIVER_JS, timeout=30)

    def test_a_project_without_machines_looks_as_it_did(self):
        self.assertIsNone(self.out["noMachines"]["picker"])
        self.assertEqual(self.out["noMachines"]["chips"], [])
        self.assertEqual(self.out["plainBody"], {"project": "plain", "taskId": "TASK-1"})

    def test_the_picker_offers_the_hub_then_the_machines_and_preselects_the_default(self):
        self.assertEqual(self.out["fresh"]["picker"], ["hub (this machine)", "box-b", "box-c"])
        self.assertEqual(self.out["fresh"]["selected"], "box-b")
        self.assertEqual(self.out["hubDefault"]["selected"], "hub")

    def test_the_choice_is_sent_as_the_machine_field(self):
        self.assertEqual(self.out["afterPick"]["selected"], "box-c")
        self.assertEqual(self.out["pickBody"], {"project": "multi", "taskId": "TASK-1", "machine": "box-c"})
        self.assertEqual(self.out["defaultBody"], {"project": "multi", "taskId": "TASK-4", "machine": "box-b"})
        self.assertEqual(self.out["hubBody"], {"project": "hubdef", "taskId": "TASK-1", "machine": "hub"})

    def test_a_task_already_on_a_worker_names_it_and_offers_no_choice(self):
        card = self.out["onWorker"]
        self.assertIsNone(card["picker"])
        self.assertEqual([c["text"] for c in card["chips"]], ["box-c"])

    def test_the_drawer_offers_the_same_picker(self):
        self.assertEqual(self.out["drawer"]["picker"], ["hub (this machine)", "box-b", "box-c"])


@js_harness.requires_node
class LocationConflictTests(unittest.TestCase):
    @property
    def out(self):
        return js_harness.cached_driver(self, SPAWN_DRIVER_JS, timeout=30)

    def test_the_card_shows_both_locations_and_why(self):
        card = self.out["conflict"]
        [chip] = [c for c in card["chips"] if "location-conflict" in c["className"]]
        self.assertEqual(chip["text"], "in hub + box-b")
        self.assertIn("two places at once: hub and box-b", chip["title"])
        self.assertIsNone(card["picker"])

    def test_the_conflicted_card_cannot_spawn(self):
        button = self.out["conflict"]["button"]
        self.assertTrue(button["disabled"])
        self.assertIn("hub and box-b", button["title"])

    def test_the_drawer_states_the_reason_and_disables_the_actions(self):
        drawer = self.out["drawerConflict"]
        self.assertEqual(drawer["picker"], 0)
        self.assertTrue(drawer["buttons"])
        for button in drawer["buttons"]:
            self.assertTrue(button["disabled"])
            self.assertIn("hub and box-b", button["title"])

    def test_the_merge_family_is_blocked_by_the_same_reason(self):
        block = self.out["blockReason"]
        self.assertIn("hub and box-b", block["conflict"])
        self.assertIsNone(block["worker"])
        self.assertIsNone(block["none"])

    def test_a_session_of_a_conflicted_task_says_so(self):
        texts = [s["text"] for s in self.out["sessions"]]
        self.assertIn("task in two places: hub + box-b", texts)
        self.assertIn("on box-c · ~/w/multi-task-2", texts)


SETTINGS_DRIVER_JS = js_harness.LOAD_SOURCES_JS + r"""
var C = loadFrontend(["state.js", "dom.js", "tasks.js", "settings.js"]);
wireSettingsShell();
C.showToast = function () {};
C.doRefresh = function () {};
C.syncDrawerPanePolling = function () {};

var requests = [];
var refuse = null;
function payload(withMachines) {
  var data = {
    harvestMode: "click", sessionPreviewMode: "view", refreshIntervalSeconds: 30,
    checkCommands: { alpha: "make test", beta: null }, defaultAgent: "claude", agents: ["claude"],
    agentEntries: [{ name: "claude", cmdText: "claude", promptSuffix: "", builtin: true, onPath: true }],
    projects: [{ name: "alpha", path: "/a" }, { name: "beta", path: "/b" }]
  };
  if (withMachines) {
    data.machines = ["box-b", "box-c"];
    data.projectMachines = { alpha: ["box-b"], beta: [] };
    data.defaultMachine = { alpha: "box-b", beta: "hub" };
  }
  return data;
}
var WITH = true;
global.fetch = function (url, opts) {
  var body = opts && opts.body ? JSON.parse(opts.body) : null;
  requests.push(body);
  if (body && refuse) {
    var fields = refuse; refuse = null;
    return Promise.resolve({ ok: false, status: 400, json: function () { return Promise.resolve({ error: "invalid settings", fields: fields }); } });
  }
  return Promise.resolve({ ok: true, status: 200, json: function () { return Promise.resolve(payload(WITH)); } });
};
function byId(id) { return document.getElementById(id); }
function group(project) { return byId("settings-check-commands").querySelectorAll('[data-setting="projectMachines"][data-project="' + project + '"]')[0]; }
function defSel(project) { return byId("settings-check-commands").querySelectorAll('[data-setting="defaultMachine"][data-project="' + project + '"]')[0]; }
function boxes(project) { return group(project).querySelectorAll("input[data-machine]"); }
function options(select) { return select.childNodes.map(function (o) { return o.value; }); }
function errs() {
  return document.querySelectorAll("#settings-modal .settings-field-error").filter(function (e) { return !e.hidden; })
    .map(function (e) { return e.textContent; });
}
var out = {};
byId("settings-open").click();
settle().then(function () {
  out.open = {
    checked: { alpha: boxes("alpha").map(function (b) { return b.checked; }), beta: boxes("beta").map(function (b) { return b.checked; }) },
    names: boxes("alpha").map(function (b) { return b.value; }),
    defaults: { alpha: defSel("alpha").value, beta: defSel("beta").value },
    options: { alpha: options(defSel("alpha")), beta: options(defSel("beta")) }
  };
  // Ticking a machine adds it to the default's options; unticking the default one falls back to the hub.
  boxes("beta")[1].checked = true; boxes("beta")[1].dispatch("change", {});
  out.afterTick = options(defSel("beta"));
  boxes("alpha")[0].checked = false; boxes("alpha")[0].dispatch("change", {});
  out.afterUntick = { options: options(defSel("alpha")), value: defSel("alpha").value };
  byId("settings-save").click();
  return settle();
}).then(function () {
  var saved = requests[requests.length - 1];
  out.saved = { projectMachines: saved.projectMachines, defaultMachine: saved.defaultMachine };
  // The server refuses a removal while tasks remain: its reason shows under that project's field.
  refuse = { "projectMachines.alpha": "box-b still has alpha's tasks: TASK-3" };
  byId("settings-save").click();
  return settle();
}).then(function () {
  out.refused = { errors: errs(), status: byId("settings-status").textContent };
  // Without machines the form has none of it and sends none of it.
  WITH = false;
  byId("settings-open").click();
  return settle();
}).then(function () {
  out.without = { groups: byId("settings-check-commands").querySelectorAll('[data-setting="projectMachines"]').length };
  byId("settings-save").click();
  return settle();
}).then(function () {
  var saved = requests[requests.length - 1];
  out.withoutBody = { has: "projectMachines" in saved || "defaultMachine" in saved };
  process.stdout.write(JSON.stringify(out));
}).catch(function (err) { process.stderr.write((err && err.stack) || String(err)); process.exit(1); });
"""


@js_harness.requires_node
class SettingsMachinesTests(unittest.TestCase):
    @property
    def out(self):
        return js_harness.cached_driver(self, SETTINGS_DRIVER_JS, timeout=30)

    def test_each_project_shows_its_machines_and_default(self):
        out = self.out["open"]
        self.assertEqual(out["names"], ["box-b", "box-c"])
        self.assertEqual(out["checked"], {"alpha": [True, False], "beta": [False, False]})
        self.assertEqual(out["defaults"], {"alpha": "box-b", "beta": "hub"})
        self.assertEqual(out["options"], {"alpha": ["", "hub", "box-b"], "beta": ["", "hub"]})

    def test_the_default_only_offers_the_machines_ticked(self):
        self.assertEqual(self.out["afterTick"], ["", "hub", "box-c"])
        self.assertEqual(self.out["afterUntick"], {"options": ["", "hub"], "value": "hub"})

    def test_save_sends_both_maps_for_every_project(self):
        self.assertEqual(self.out["saved"], {
            "projectMachines": {"alpha": [], "beta": ["box-c"]},
            "defaultMachine": {"alpha": "hub", "beta": "hub"}})

    def test_the_servers_refusal_lands_under_the_projects_machines(self):
        self.assertEqual(self.out["refused"]["errors"], ["box-b still has alpha's tasks: TASK-3"])
        self.assertEqual(self.out["refused"]["status"], "Fix the highlighted field and try again.")

    def test_without_machines_the_form_is_unchanged(self):
        self.assertEqual(self.out["without"]["groups"], 0)
        self.assertFalse(self.out["withoutBody"]["has"])


if __name__ == "__main__":
    unittest.main()


@js_harness.requires_node
class FleetSplitCardTests(unittest.TestCase):
    def test_a_project_running_in_two_places_has_a_card_in_each_group(self):
        import test_frontend_behaviour as tfb

        driver = tfb.FLEET_VIEWS_DRIVER_JS.replace(
            "function find(node, cls, out) {",
            """SNAP.projects = [{ name: "atlas", maxAgents: 3, agentCount: 2, machines: ["box-b"] }];
SNAP.agents[1].machine = "box-b";  // TASK-1 stays on the hub, TASK-2 runs on box-b
SNAP.machines = [{ name: "box-b", reach: "reachable", error: null, agentCount: 1, maxAgents: 8 }];
SNAP.hub = { agentCount: 1 };
function find(node, cls, out) {""", 1)
        out = js_harness.cached_driver(self, driver, "view-fleet.js")
        self.assertEqual([(g["machine"], g["cards"]) for g in out["machineGroups"]],
                         [("hub", 1), ("box-b", 1)])
        self.assertEqual(sorted(r["task"] for r in out["rows"]), ["TASK-1", "TASK-2"])


WORKER_SETTINGS_DRIVER_JS = SETTINGS_DRIVER_JS.replace(
    'checkCommands: { alpha: "make test", beta: null },',
    'workerCheck: { alpha: { command: "scripts/check.sh --check", doc: "docs/setup.md" } }, checkCommands: { alpha: "make test", beta: null },'
).replace(
    '  // Ticking a machine adds',
    '''  function workerField(key) { return document.querySelector('[data-setting="workerCheck' + (key === "command" ? "Command" : "Doc") + '"][data-project="alpha"]'); }
  out.workerLoaded = { command: workerField("command").value, doc: workerField("doc").value };
  workerField("command").value = "scripts/ready.sh";
  workerField("doc").value = "docs/worker.md";
  // Ticking a machine adds'''
).replace(
    '  out.saved = { projectMachines:',
    '  out.workerSaved = saved.workerCheck;\n  out.saved = { projectMachines:'
).replace(
    'refuse = { "projectMachines.alpha": "box-b still has alpha\'s tasks: TASK-3" };',
    'refuse = { "workerCheck.alpha": "command and doc required" };'
)


@js_harness.requires_node
class WorkerSettingsTests(unittest.TestCase):
    def test_load_save_and_pair_error(self):
        out = js_harness.run_driver(self, WORKER_SETTINGS_DRIVER_JS)
        self.assertEqual(out['workerLoaded'], {'command': 'scripts/check.sh --check', 'doc': 'docs/setup.md'})
        self.assertEqual(out['workerSaved'], {'alpha': {'command': 'scripts/ready.sh', 'doc': 'docs/worker.md'}, 'beta': None})
        self.assertEqual(out['refused']['errors'], ['command and doc required'])


@js_harness.requires_node
class SettingsAutomaticPlacementTests(unittest.TestCase):
    def test_absent_default_survives_open_and_save_as_automatic(self):
        driver = SETTINGS_DRIVER_JS.replace(
            'data.defaultMachine = { alpha: "box-b", beta: "hub" };',
            'data.defaultMachine = { alpha: null, beta: "hub" };')
        out = js_harness.run_driver(self, driver, timeout=30)
        self.assertEqual(out['open']['defaults']['alpha'], '')
        self.assertIsNone(out['saved']['defaultMachine']['alpha'])
        self.assertEqual(out['saved']['defaultMachine']['beta'], 'hub')


@js_harness.requires_node
class MachineLimitSettingsTests(unittest.TestCase):
    def test_limits_load_save_clear_and_follow_machine_selection(self):
        driver = SETTINGS_DRIVER_JS.replace(
            'harvestMode: "click", sessionPreviewMode:',
            'hubMaxAgents: 8, machineMaxAgents: {alpha: {hub: 2, "box-b": 3}}, harvestMode: "click", sessionPreviewMode:'
        ).replace('  // Ticking a machine adds', r'''  function cap(project, machine) { return document.querySelector('input[data-project="' + project + '"][data-limit-machine="' + machine + '"]'); }
  out.limitsLoaded = [byId("settings-hub-max-agents").value, cap("alpha", "hub").value, cap("alpha", "box-b").value, cap("beta", "box-c").disabled];
  byId("settings-hub-max-agents").value = "9";
  cap("alpha", "hub").value = "";
  cap("beta", "box-c").value = "4";
  // Ticking a machine adds''').replace(
            '  out.saved = { projectMachines:',
            '  out.limitsSaved = {hub: saved.hubMaxAgents, projects: saved.machineMaxAgents};\n  out.saved = { projectMachines:'
        ).replace('  out.withoutBody = { has:',
                  '  out.soloLimits = saved.machineMaxAgents;\n  out.withoutBody = { has:')
        out = js_harness.run_driver(self, driver, timeout=30)
        self.assertEqual(out['limitsLoaded'], ['8', '2', '3', True])
        self.assertEqual(out['limitsSaved'], {'hub': 9, 'projects': {'alpha': {}, 'beta': {'box-c': 4}}})
        self.assertEqual(out['soloLimits'], {'alpha': {'hub': 2}, 'beta': {}})
