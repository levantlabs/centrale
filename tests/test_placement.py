"""Per-task placement (doc-3 revision 5, task-224): a project lists the
machines its tasks may run on, each spawn picks one, and a task's
location is derived from where its worktree (or branch) is -- and every
per-task action follows that location.

Hermetic: the hub's git (server.run_git), each worker (machines.run_batch)
and the remote modules' entry points are faked by World; no ssh, git,
tmux or agent runs here. The worker scripts run for real in
tests_integration/test_remote_integration.py, which also spawns and
merges one task on the hub and one on a worker for the same project."""

import contextlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import harvest  # noqa: E402
import machines  # noqa: E402
import remote  # noqa: E402
import remote_harvest  # noqa: E402
import server  # noqa: E402
import spawn  # noqa: E402
from machines import ANSWERED, UNREACHED, MachineResult  # noqa: E402

BOX = {"name": "box-b", "ssh": "user@box-b", "repoRoot": "~/repos", "worktreeRoot": "~/work",
       "maxAgents": 4}
OTHER = {"name": "box-c", "ssh": "box-c", "repoRoot": "/srv/repos", "worktreeRoot": "/srv/work",
         "maxAgents": None}
HOME = "/home/user"


class World:
    """The hub's git and each worker's answers, from plain state."""

    def __init__(self, repo):
        self.repo = repo
        self.hub_branches = set()      # task/<id> on the hub
        self.hub_sessions = []         # centrale-* sessions in the hub's tmux
        self.fetched = {"box-b": set(), "box-c": set()}
        self.workers = {name: {"worktrees": {}, "branches": set(), "sessions": [], "reach": ANSWERED}
                        for name in ("box-b", "box-c")}
        self.asked = []                # machine names asked, in order

    def run_git(self, args, cwd=None):
        out, rc = "", 0
        if args[:3] == ["rev-parse", "--verify", "--quiet"]:
            ref = args[3]
            if ref.startswith("refs/heads/"):
                rc = 0 if ref[len("refs/heads/"):] in self.hub_branches else 1
            elif ref.startswith("refs/remotes/"):
                _r, _rs, machine, branch = ref.split("/", 3)
                rc = 0 if branch in self.fetched.get(machine, ()) else 1
            out = "f" * 40 + "\n" if rc == 0 else ""
        elif args[:3] == ["worktree", "list", "--porcelain"]:
            out = f"worktree {self.repo}\nHEAD abc\nbranch refs/heads/main\n"
        elif args[:1] == ["for-each-ref"]:
            prefix = args[-1]
            if prefix == "refs/heads/task":
                out = "".join(f"{b}\n" for b in sorted(self.hub_branches))
            elif prefix.startswith("refs/remotes/"):
                machine = prefix.split("/")[2]
                out = "".join(f"refs/remotes/{machine}/{b}\n" for b in sorted(self.fetched.get(machine, ())))
        elif args[:1] == ["remote"]:
            out = ""
        return subprocess.CompletedProcess(args, rc, out, "")

    def run_batch(self, config, name, commands, timeout=None, input=None):
        self.asked.append(name)
        worker = self.workers[name]
        results = []
        for argv, _cwd in commands:
            argv = [str(a) for a in argv]
            if worker["reach"] != ANSWERED:
                results.append(MachineResult(UNREACHED, None, "", "ssh: connect timed out", name))
                continue
            script = argv[2] if argv[:2] == ["sh", "-c"] else None
            if script == machines._WORKTREE_LIST:
                out = "".join(f"worktree {p}\nHEAD abc\nbranch refs/heads/{b}\n\n"
                              for b, p in worker["worktrees"].items())
            elif script == machines._BRANCH_LIST:
                out = "".join(f"refs/heads/{b}\n" for b in sorted(worker["branches"] | set(worker["worktrees"])))
            elif script == remote._SESSIONS_SCRIPT:
                out = "".join(f"{s}\t100\n" for s in worker["sessions"])
            elif script == remote._HOME_SCRIPT:
                out = HOME
            elif argv == ["true"]:  # a refresh's reachability probe
                out = ""
            else:
                raise AssertionError(f"unexpected worker command {argv!r}")
            results.append(MachineResult(ANSWERED, 0, out, "", name))
        return results

    def patches(self):
        return [mock.patch.object(server, "run_git", self.run_git),
                mock.patch.object(machines, "run_batch", self.run_batch),
                mock.patch.object(server, "list_sessions", lambda *a, **k: [
                    {"name": n, "created": "1", "attached": False} for n in self.hub_sessions])]


class PlacementCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = tmp.name
        self.world = World(self.repo)
        self.config = {"port": 0, "worktreeRoot": os.path.join(tmp.name, "wt"), "lockSpawnedTaskFiles": False,
                       "machines": [dict(BOX), dict(OTHER)], "refreshIntervalSeconds": 10,
                       "projects": [{"name": "app", "path": self.repo, "machines": ["box-b"],
                                     "defaultMachine": None, "maxAgents": None, "worktreeLinks": []},
                                    {"name": "solo", "path": self.repo, "machines": [],
                                     "defaultMachine": None, "maxAgents": None, "worktreeLinks": []}]}
        self.project = self.config["projects"][0]
        for patcher in self.world.patches():
            patcher.start()
            self.addCleanup(patcher.stop)

    def on_hub(self, task_id):
        self.world.hub_branches.add(spawn.branch_name(task_id))

    def on_box(self, task_id, worktree=True):
        branch = spawn.branch_name(task_id)
        if worktree:
            self.world.workers["box-b"]["worktrees"][branch] = f"{HOME}/work/app-{task_id.lower()}"
        else:
            self.world.workers["box-b"]["branches"].add(branch)


class PlacementTests(PlacementCase):
    def place(self, task_id="TASK-7", requested=None, project="app", resume=False):
        return spawn.placement(self.config, project, task_id, requested, resume=resume)

    def test_a_new_task_goes_where_asked_else_the_default_else_the_hub(self):
        self.assertIsNone(self.place())
        self.assertEqual(self.place(requested="box-b"), "box-b")
        self.project["defaultMachine"] = "box-b"
        self.assertEqual(self.place(), "box-b")
        self.assertIsNone(self.place(requested="hub"))

    def test_a_machine_the_project_does_not_list_is_refused_with_the_allowed_list(self):
        for requested in ("box-c", "box-z", ""):
            with self.subTest(requested=requested), self.assertRaises(spawn.SpawnError) as caught:
                self.place(requested=requested)
            self.assertEqual(caught.exception.status, 400)
            self.assertIn("(allowed: hub, box-b)", caught.exception.message)
        with self.assertRaises(spawn.SpawnError) as caught:
            self.place(requested=["box-b"])
        self.assertEqual(caught.exception.status, 400)

    def test_a_project_without_machines_refuses_a_machine_and_runs_nothing(self):
        with mock.patch.object(server, "run_git", side_effect=AssertionError("git")), \
             mock.patch.object(machines, "run_batch", side_effect=AssertionError("ssh")):
            self.assertIsNone(self.place(project="solo"))
            self.assertIsNone(self.place(project="solo", requested="hub"))
            with self.assertRaises(spawn.SpawnError) as caught:
                self.place(project="solo", requested="box-b")
        self.assertIn("(allowed: hub)", caught.exception.message)

    def test_a_task_with_a_location_goes_there_and_an_explicit_elsewhere_is_refused(self):
        self.on_hub("TASK-7")
        self.project["defaultMachine"] = "box-b"
        self.assertIsNone(self.place())  # its branch is on the hub: it stays there
        with self.assertRaises(spawn.SpawnError) as caught:
            self.place(requested="box-b")
        self.assertEqual(caught.exception.status, 409)
        self.assertIn("TASK-7 is already on the hub", caught.exception.message)
        self.on_box("TASK-8", worktree=False)
        self.assertEqual(self.place("TASK-8"), "box-b")
        with self.assertRaises(spawn.SpawnError) as caught:
            self.place("TASK-8", requested="hub", resume=True)
        self.assertIn("already on machine box-b", caught.exception.message)
        self.assertIn("resume it there", caught.exception.message)

    def test_a_task_in_two_places_is_refused_naming_both(self):
        self.on_hub("TASK-7")
        self.on_box("TASK-7")
        with self.assertRaises(spawn.SpawnError) as caught:
            self.place()
        self.assertEqual(caught.exception.status, 409)
        self.assertIn("the hub and machine box-b", caught.exception.message)

    def test_an_unreachable_machine_still_holds_its_tasks_by_their_fetched_ref(self):
        self.world.workers["box-b"]["reach"] = UNREACHED
        self.world.fetched["box-b"].add("task/task-7")
        self.assertEqual(self.place(), "box-b")
        with self.assertRaises(spawn.SpawnError):
            self.place(requested="hub")

    def test_each_machine_is_asked_once_per_placement(self):
        self.project["machines"] = ["box-b", "box-c"]
        self.place()
        self.assertEqual(sorted(self.world.asked), ["box-b", "box-c"])


class SpawnDispatchTests(PlacementCase):
    def test_spawn_and_resume_launch_where_the_placement_says(self):
        calls = []
        with mock.patch.object(remote, "spawn_on_machine", lambda *a, **k: calls.append(("spawn", k)) or {}), \
             mock.patch.object(remote, "resume_on_machine", lambda *a, **k: calls.append(("resume", k)) or {}), \
             mock.patch.object(spawn, "_validate_and_check_session",
                               side_effect=spawn.SpawnError("the hub path", status=418)):
            spawn.spawn(self.config, "app", "TASK-7", machine="box-b")
            self.on_box("TASK-8")
            spawn.resume(self.config, "app", "TASK-8")
            with self.assertRaises(spawn.SpawnError) as caught:
                spawn.spawn(self.config, "app", "TASK-9", machine="hub")
        self.assertEqual(calls, [("spawn", {"agent": None, "machine_name": "box-b"}),
                                 ("resume", {"reconcile": False, "machine_name": "box-b"})])
        self.assertEqual(caught.exception.status, 418)

    def test_a_projects_cap_counts_its_agents_on_every_machine(self):
        self.project["maxAgents"] = 2
        self.world.hub_sessions = ["centrale-app-task-1"]
        self.world.workers["box-b"]["sessions"] = ["centrale-app-task-2", "centrale-other-task-3"]
        with mock.patch.object(spawn, "_validate_request", lambda c, p, t: self.project):
            with self.assertRaises(spawn.SpawnError) as caught:
                spawn._validate_and_check_session(self.config, "app", "TASK-7")
        self.assertIn("2 live agent sessions", caught.exception.message)
        machine = machines.machine_named(self.config, "box-b")
        placed = machines.placed(self.project, "box-b")
        with self.assertRaises(spawn.SpawnError) as caught:
            remote._check_capacity(self.config, machine, placed, "centrale-app-task-7",
                                   remote._parse_sessions("centrale-app-task-2\t1\n"))
        self.assertIn("across its machines (maxAgents: 2)", caught.exception.message)
        self.project["maxAgents"] = None
        self.world.hub_sessions = []
        with mock.patch.object(spawn, "_validate_request", lambda c, p, t: self.project):
            spawn._validate_and_check_session(self.config, "app", "TASK-7")

    def test_a_machines_own_cap_is_checked_too(self):
        machine = dict(machines.machine_named(self.config, "box-b"), maxAgents=1)
        with self.assertRaises(spawn.SpawnError) as caught:
            remote._check_capacity(self.config, machine, machines.placed(self.project, "box-b"),
                                   "centrale-app-task-7", remote._parse_sessions("centrale-app-task-2\t1\n"))
        self.assertIn("Machine box-b has 1 live agent sessions (maxAgents: 1)", caught.exception.message)


class MachineLimitTests(PlacementCase):
    def setUp(self):
        super().setUp()
        self.config["capabilities"] = {"tmux": True}

    def test_bound_worker_launch_does_not_wait_on_held_hub_lock(self):
        self.config["hubMaxAgents"] = 1
        for launch, remote_name in ((spawn.spawn, "spawn_on_machine"),
                                    (spawn.resume, "resume_on_machine")):
            for binding in ("explicit", "default", "located"):
                with self.subTest(launch=launch.__name__, binding=binding):
                    self.project["defaultMachine"] = "box-b" if binding == "default" else None
                    self.world.workers["box-b"]["worktrees"].clear()
                    if binding == "located":
                        self.on_box("TASK-7")
                    arrived = threading.Event()
                    errors = []
                    def run():
                        try:
                            launch(self.config, "app", "TASK-7",
                                   machine="box-b" if binding == "explicit" else None)
                        except Exception as exc:
                            errors.append(exc)
                    with mock.patch.object(remote, remote_name,
                                           side_effect=lambda *a, **kw: arrived.set() or {}):
                        spawn._hub_capacity_lock.acquire()
                        thread = threading.Thread(target=run)
                        try:
                            thread.start()
                            progressed = arrived.wait(1)
                        finally:
                            spawn._hub_capacity_lock.release()
                            thread.join(5)
                        self.assertFalse(thread.is_alive())
                        self.assertEqual(errors, [])
                        self.assertTrue(progressed, "worker launch waited on the hub lock")

    def test_hub_limits_refuse_spawn_and_resume_with_live_names(self):
        for setting in ("hubMaxAgents", "machineMaxAgents"):
            for launch in (spawn.spawn, spawn.resume):
                with self.subTest(setting=setting, launch=launch.__name__):
                    self.config.pop("hubMaxAgents", None)
                    self.project.pop("machineMaxAgents", None)
                    if setting == "hubMaxAgents":
                        self.config[setting] = 1
                        self.world.hub_sessions = ["centrale-solo-task-1"]
                    else:
                        self.project[setting] = {"hub": 1}
                        self.world.hub_sessions = ["centrale-app-task-1"]
                    with self.assertRaises(spawn.SpawnError) as caught:
                        launch(self.config, "app", "TASK-7", machine="hub")
                    self.assertEqual(caught.exception.status, 409)
                    self.assertIn(setting + ": 1", caught.exception.message)
                    self.assertIn(self.world.hub_sessions[0], caught.exception.message)

    def test_worker_project_limit_refuses_spawn_and_resume(self):
        self.project["machineMaxAgents"] = {"box-b": 1}
        self.world.workers["box-b"]["sessions"] = ["centrale-app-task-1"]
        for launch in (spawn.spawn, spawn.resume):
            with self.subTest(launch=launch.__name__), self.assertRaises(spawn.SpawnError) as caught:
                launch(self.config, "app", "TASK-7", machine="box-b")
            self.assertEqual(caught.exception.status, 409)
            self.assertIn("machineMaxAgents: 1", caught.exception.message)
            self.assertIn("centrale-app-task-1", caught.exception.message)

    def test_limits_are_scoped_to_machine_and_project(self):
        self.config["hubMaxAgents"] = 1
        self.project["machineMaxAgents"] = {"hub": 1, "box-b": 1}
        self.world.hub_sessions = ["centrale-solo-task-1"]
        remote._check_capacity(self.config, self.config["machines"][0], self.project,
                               "centrale-app-task-7", [{"name": "centrale-solo-task-2"}])
        self.project["machineMaxAgents"] = {"box-b": 1}
        self.config.pop("hubMaxAgents")
        spawn._validate_and_check_session(self.config, "app", "TASK-7")
        remote._check_capacity(self.config, self.config["machines"][1], self.project,
                               "centrale-app-task-7", [{"name": "centrale-app-task-2"}])


class MixedProjectHttpTests(PlacementCase):
    """One project with TASK-1 on the hub, TASK-2 on box-b and TASK-3 in
    both places at once: every route follows each task's location."""

    def setUp(self):
        super().setUp()
        self.on_hub("TASK-1")
        self.on_box("TASK-2")
        self.on_hub("TASK-3")
        self.on_box("TASK-3")
        self.httpd = server.CentraleHTTPServer(("127.0.0.1", 0), server.Handler, self.config)
        thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(lambda: (self.httpd.shutdown(), self.httpd.server_close(), thread.join(5)))
        self.remote_calls = []

    def request(self, method, path, body=None):
        url = f"http://127.0.0.1:{self.httpd.server_address[1]}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode())

    def record(self, name, answer=(200, {"ok": True})):
        def call(config, project, *args):
            self.remote_calls.append((name, project.get("machine"), args[0]))
            return answer
        return call

    @contextlib.contextmanager
    def hub_routes(self):
        hub = []
        with mock.patch.object(server.Handler, "_cleanup_branch_locked",
                               lambda h, project, name, task, repo, force=False: (
                                   hub.append(("cleanup", task)), h._send_json(200, {"hub": True}))), \
             mock.patch.object(server.Handler, "_survey_attempt",
                               lambda h, project, task: (hub.append(("survey", task)),
                                                         h._send_error_json(418, "hub survey"))[1]), \
             mock.patch.object(server, "run_tmux", lambda args, **k: (
                 hub.append(("tmux", args[0])),
                 subprocess.CompletedProcess(args, 1, "", "no server running"))[1]):
            yield hub

    def test_end_session_follows_each_tasks_location(self):
        with mock.patch.object(remote, "end_session", self.record("end")), self.hub_routes() as hub:
            self.assertEqual(self.request("POST", "/api/end-session", {"project": "app", "taskId": "TASK-2"})[0], 200)
            hub_end = self.request("POST", "/api/end-session", {"project": "app", "taskId": "TASK-1"})
            conflict = self.request("POST", "/api/end-session", {"project": "app", "taskId": "TASK-3"})
        self.assertEqual(self.remote_calls, [("end", "box-b", "TASK-2")])
        # The hub's own tmux was asked, and had no such session.
        self.assertEqual(hub_end, (404, {"error": "no live session for app/TASK-1"}))
        self.assertEqual(hub, [])
        self.assertEqual(conflict[0], 409)
        self.assertIn("the hub and machine box-b", conflict[1]["error"])

    def test_cleanup_follows_each_tasks_location(self):
        with mock.patch.object(remote, "cleanup", self.record("cleanup")), self.hub_routes() as hub:
            for task in ("TASK-1", "TASK-2"):
                self.assertEqual(self.request("POST", "/api/cleanup-branch",
                                              {"project": "app", "taskId": task})[0], 200)
            conflict = self.request("POST", "/api/cleanup-branch", {"project": "app", "taskId": "TASK-3"})
        self.assertEqual(self.remote_calls, [("cleanup", "box-b", "TASK-2")])
        self.assertEqual(hub, [("cleanup", "TASK-1")])
        self.assertEqual(conflict[0], 409)

    def test_remote_cleanup_preserves_dirty_path_conflict_body(self):
        refusal = {"error": "worktree has uncommitted changes", "discardedPaths": ["notes with spaces.txt"]}
        with mock.patch.object(remote, "cleanup", self.record("cleanup", (409, refusal))):
            status, body = self.request("POST", "/api/cleanup-branch",
                                        {"project": "app", "taskId": "TASK-2"})
        self.assertEqual(status, 409)
        self.assertEqual(body, refusal)

    def test_discard_preview_discard_and_abandon_follow_each_tasks_location(self):
        expected = {"expectedBranchTip": None, "expectedDirtyPaths": []}
        with mock.patch.object(remote_harvest, "discard_preview", self.record("preview")), \
             mock.patch.object(remote_harvest, "throw_away", self.record("throw")), self.hub_routes() as hub:
            self.assertEqual(self.request("GET", "/api/discard-preview?project=app&task=TASK-2")[0], 200)
            self.assertEqual(self.request("GET", "/api/discard-preview?project=app&task=TASK-1")[0], 418)
            self.assertEqual(self.request("POST", "/api/discard-attempt",
                                          {"project": "app", "taskId": "TASK-2", **expected})[0], 200)
            self.assertEqual(self.request("POST", "/api/abandon-worktree",
                                          {"project": "app", "taskId": "TASK-2", **expected})[0], 200)
            refused = [self.request("GET", "/api/discard-preview?project=app&task=TASK-3"),
                       self.request("POST", "/api/discard-attempt", {"project": "app", "taskId": "TASK-3", **expected}),
                       self.request("POST", "/api/abandon-worktree", {"project": "app", "taskId": "TASK-3", **expected})]
        self.assertEqual([c[0] for c in self.remote_calls], ["preview", "throw", "throw"])
        self.assertEqual({c[1] for c in self.remote_calls}, {"box-b"})
        self.assertEqual(hub, [("survey", "TASK-1")])
        self.assertEqual([r[0] for r in refused], [409, 409, 409])

    def test_merge_gates_and_merge_follow_each_tasks_location(self):
        def remote_merge(config, project, name, task):
            self.remote_calls.append(("merge", project["machine"], task))
            return {"taskId": task, "merged": True, "gates": [], "machine": project["machine"]}

        def hub_merge(config, project, name, task):
            self.remote_calls.append(("merge", project.get("machine"), task))
            return {"taskId": task, "merged": True, "gates": []}

        with mock.patch.object(remote_harvest, "harvest_locked", remote_merge), \
             mock.patch.object(harvest, "_harvest_branch_locked", hub_merge), \
             mock.patch.object(harvest, "_record_event", lambda *a, **k: None):
            for task in ("TASK-1", "TASK-2"):
                self.assertEqual(self.request("POST", "/api/harvest", {"project": "app", "taskId": task})[0], 200)
            conflict = self.request("POST", "/api/harvest", {"project": "app", "taskId": "TASK-3"})
            report = harvest.evaluate_branch(self.config, self.project, "TASK-3")
        self.assertEqual(self.remote_calls, [("merge", None, "TASK-1"), ("merge", "box-b", "TASK-2")])
        self.assertEqual(conflict[0], 409)
        self.assertIn("in two places at once", conflict[1]["error"])
        self.assertFalse(report["harvestable"])
        self.assertIn("in two places at once", report["gates"][0]["reason"])

    def test_task_responses_carry_the_location(self):
        view = {"schemaVersion": 1, "task": {"id": "TASK-1"}}
        branch_view = {"schemaVersion": 1, "task": {"id": "TASK-2", "assignees": ["@codex"]}}
        with mock.patch.object(server, "run_backlog", lambda *a, **k: view), \
             mock.patch.object(server, "_branch_task_view", lambda *a, **k: branch_view if a[2] == "TASK-2" else None), \
             mock.patch.object(machines, "run_batch", side_effect=AssertionError("no ssh on a drawer open")):
            self.world.fetched["box-b"].add("task/task-2")
            self.world.fetched["box-b"].add("task/task-3")
            bodies = {t: self.request("GET", f"/api/task?project=app&id={t}")[1]
                      for t in ("TASK-1", "TASK-2", "TASK-3")}
        self.assertEqual(bodies["TASK-1"]["location"], {"machine": "hub", "conflict": None, "reach": None})
        self.assertNotIn("machine", bodies["TASK-1"])
        self.assertEqual(bodies["TASK-2"]["location"]["machine"], "box-b")
        self.assertEqual((bodies["TASK-2"]["machine"], bodies["TASK-2"]["worktree"]), ("box-b", "~/work/app-task-2"))
        self.assertEqual(bodies["TASK-3"]["location"]["conflict"], ["hub", "box-b"])
        self.assertNotIn("machine", bodies["TASK-3"])
        self.assertEqual(bodies["TASK-2"]["resumeAgent"], "codex")

    def test_spawn_refuses_an_unlisted_machine_over_http(self):
        status, body = self.request("POST", "/api/spawn", {"project": "app", "taskId": "TASK-9", "machine": "box-c"})
        self.assertEqual(status, 400)
        self.assertIn("allowed: hub, box-b", body["error"])


class DrawerReadTests(PlacementCase):
    def test_a_task_on_a_worker_is_read_from_the_hubs_fetched_copy(self):
        self.on_box("TASK-2")
        self.world.fetched["box-b"].add("task/task-2")
        snapshots = []

        @contextlib.contextmanager
        def snapshot(repo, ref):
            snapshots.append(ref)
            yield "/tmp/snap", None

        location = server.task_location(self.config, self.project, "TASK-2", live=False)
        with mock.patch.object(spawn, "detached_snapshot", snapshot), \
             mock.patch.object(server, "_task_view_or_none", lambda path, task: {"task": {"status": "Done"}}):
            view = server._branch_task_view(self.config, self.project, "TASK-2", location=location)
            self.on_hub("TASK-2")  # now in two places: nothing is read
            conflict = server.task_location(self.config, self.project, "TASK-2", live=False)
            self.assertIsNone(server._branch_task_view(self.config, self.project, "TASK-2", location=conflict))
        self.assertEqual(view, {"task": {"status": "Done"}})
        self.assertEqual(snapshots, ["refs/remotes/box-b/task/task-2"])


class BoardPlacementTests(PlacementCase):
    def test_the_board_locates_each_task_from_one_round_trip_per_machine(self):
        self.on_hub("TASK-1")
        self.on_box("TASK-2")
        self.on_hub("TASK-3")
        self.on_box("TASK-3")
        self.world.workers["box-b"]["branches"].add("task/task-4")
        self.world.fetched["box-b"].add("task/task-4")  # only its fetched ref: still box-b
        tasks = [{"id": f"TASK-{n}", "isReady": False, "status": "In Progress"} for n in range(1, 6)]
        with mock.patch.object(server, "_load_board_inputs", lambda path: ({"tasks": tasks}, {})), \
             mock.patch.object(server, "_read_statuses", lambda path: None), \
             mock.patch.object(remote, "fetch_task_branches", lambda *a, **k: None), \
             mock.patch.object(remote, "owed_teardowns", lambda: []):
            board = server.get_board(self.config)
        entry = board["projects"][0]
        by_id = {t["id"]: t for t in entry["tasks"]}
        self.assertEqual(entry["machines"], ["box-b"])
        self.assertEqual(entry["defaultMachine"], "hub")
        self.assertEqual([f["machine"] for f in entry["fetches"]], ["box-b"])
        self.assertEqual(by_id["TASK-1"]["location"]["machine"], "hub")
        self.assertNotIn("machine", by_id["TASK-1"])
        self.assertEqual((by_id["TASK-2"]["location"]["machine"], by_id["TASK-2"]["machine"]), ("box-b", "box-b"))
        self.assertEqual(by_id["TASK-2"]["branchCheckout"]["kind"], "centrale")
        self.assertTrue(by_id["TASK-2"]["hasSpawnBranch"])
        self.assertEqual(by_id["TASK-3"]["location"]["conflict"], ["hub", "box-b"])
        self.assertFalse(by_id["TASK-3"]["alreadyMerged"])
        self.assertEqual(by_id["TASK-4"]["location"]["machine"], "box-b")
        self.assertEqual(by_id["TASK-4"]["branchCheckout"]["kind"], "none")
        self.assertEqual(by_id["TASK-5"]["location"], {"machine": None, "conflict": None, "reach": None})
        self.assertFalse(by_id["TASK-5"]["hasSpawnBranch"])
        # The project without machines carries none of it.
        self.assertFalse({"location", "machine"} & set(board["projects"][1]["tasks"][0]))
        self.assertNotIn("machines", board["projects"][1])
        self.assertEqual(self.world.asked.count("box-b"), 1)




class AutoChoiceTests(unittest.TestCase):
    def row(self, name, ram=None, load=None, cores=None, **extra):
        return dict(name=name, reachable=True, ramAvailable=ram, load1=load,
                    cores=cores, **extra)

    def test_most_ram_then_lower_load_per_core_and_stable_ties(self):
        rows = [self.row('hub', 1e9, 0, 8), self.row('box-b', 5e9, 4, 4),
                self.row('box-c', 5e9, 2, 8)]
        chosen, reason = spawn.choose_placement(rows)
        self.assertEqual(chosen, 'box-c')
        self.assertIn('hub skipped:', reason)
        self.assertIn('box-b skipped:', reason)
        self.assertIn('load/core', reason)
        rows[1]['load1'] = 1
        self.assertEqual(spawn.choose_placement(rows)[0], 'box-b')

    def test_unknown_figures_rank_last_but_are_eligible(self):
        unknown = self.row('box-b')
        self.assertEqual(spawn.choose_placement([unknown, self.row('hub', 0)])[0], 'hub')
        self.assertEqual(spawn.choose_placement([unknown])[0], 'box-b')
        self.assertIn('unknown', spawn.choose_placement([unknown])[1])
        self.assertEqual(spawn.choose_placement([
            self.row('hub', 10), self.row('box-b', 10, 0, 4)])[0], 'box-b')

    def test_every_filter_and_empty_result_names_reasons(self):
        rows = [dict(self.row('hub', 100), reachable=False),
                self.row('box-b', 200, capacityReason='maxAgents: 2'),
                self.row('box-c', 300, workerCheckReason='missing compiler')]
        chosen, reason = spawn.choose_placement(rows)
        self.assertIsNone(chosen)
        for part in ('hub', 'unreachable', 'box-b', 'maxAgents: 2', 'box-c', 'missing compiler'):
            self.assertIn(part, reason)
        self.assertIsNone(spawn.choose_placement([])[0])
        rows.append(self.row('box-d'))
        self.assertEqual(spawn.choose_placement(rows)[0], 'box-d')


class AutoSpawnHttpTests(PlacementCase):
    request = MixedProjectHttpTests.request

    def setUp(self):
        super().setUp()
        import system_status
        self.config['capabilities'] = {'tmux': True}
        self.project['machines'] = ['box-b', 'box-c']
        self.figures = {'hub': dict(ramAvailable=1e9, load1=0, cores=4),
                        'box-b': dict(name='box-b', reachable=True, ramAvailable=5e9, load1=1, cores=4),
                        'box-c': dict(name='box-c', reachable=True, ramAvailable=3e9, load1=0, cores=4)}
        self.launched = []
        patches = [mock.patch.object(server, 'run_tmux', lambda args, **kw: subprocess.CompletedProcess(args, 0, '', '')),
                   mock.patch.object(spawn, '_claim_for_spawn', return_value=[]),
                   mock.patch.object(system_status, 'read_local', lambda p: self.figures['hub']),
                   mock.patch.object(system_status, 'read_worker', lambda c, m: self.figures[m['name']]),
                   mock.patch.dict(os.environ, {'CENTRALE_SPAWN_CMD': 'true'}),
                   mock.patch.object(remote, 'spawn_on_machine', self.launch)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.httpd = server.CentraleHTTPServer(('127.0.0.1', 0), server.Handler, self.config)
        thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(lambda: (self.httpd.shutdown(), self.httpd.server_close(), thread.join(5)))

    def launch(self, config, project, task, **kw):
        self.launched.append(kw['machine_name'])
        return {'session': 'centrale-app-task-7', 'machine': kw['machine_name']}

    def post(self, **extra):
        return self.request('POST', '/api/spawn', dict(project='app', taskId='TASK-7', **extra))

    def test_auto_placed_reply_names_choice_and_skipped_machines(self):
        status, body = self.post()
        self.assertEqual(status, 200, body)
        self.assertEqual(self.launched, ['box-b'])
        self.assertEqual(body['machine'], 'box-b')
        self.assertIn('hub skipped:', body['placementReason'])
        self.assertIn('box-c skipped:', body['placementReason'])

    def test_new_limits_skip_full_candidates_and_explain_no_capacity(self):
        self.config['hubMaxAgents'] = 1
        self.world.hub_sessions = ['centrale-solo-task-1']
        self.project['machineMaxAgents'] = {'box-b': 1}
        self.world.workers['box-b']['sessions'] = ['centrale-app-task-2']
        status, body = self.post()
        self.assertEqual(status, 200, body)
        self.assertEqual(body['machine'], 'box-c')
        for limit in ('hubMaxAgents: 1', 'machineMaxAgents: 1'):
            self.assertIn(limit, body['placementReason'])
        self.project['machineMaxAgents']['box-c'] = 1
        self.world.workers['box-c']['sessions'] = ['centrale-app-task-3']
        status, body = self.post()
        self.assertEqual(status, 409, body)
        for part in ('hub skipped:', 'box-b skipped:', 'box-c skipped:', 'hubMaxAgents', 'machineMaxAgents'):
            self.assertIn(part, body['error'])

    def test_project_full_refuses_every_candidate_before_launch(self):
        self.project['maxAgents'] = 1
        self.world.hub_sessions = ['centrale-app-task-1']
        status, body = self.post()
        self.assertEqual(status, 409, body)
        for part in ('hub', 'box-b', 'box-c', 'maxAgents'):
            self.assertIn(part, body['error'])
        self.assertEqual(self.launched, [])

    def test_full_unreachable_and_failed_check_workers_are_excluded(self):
        self.config['machines'][0]['maxAgents'] = 1
        self.world.workers['box-b']['sessions'] = ['centrale-app-task-1']
        self.project['workerCheck'] = {'command': 'check.sh', 'doc': 'setup.md'}
        with mock.patch.object(remote, 'prepare_worker_readiness', return_value=(True, 'passed')), \
             mock.patch.object(remote, 'check_hub_readiness', return_value=(False, 'missing tool')):
            status, body = self.post()
        self.assertEqual(status, 200, body)
        self.assertEqual(body['machine'], 'box-c')
        self.assertIn('maxAgents', body['placementReason'])
        self.assertIn('missing tool', body['placementReason'])

    def test_explicit_default_existing_location_and_hub_only_skip_auto_probes(self):
        import system_status
        with mock.patch.object(system_status, 'read_local', side_effect=AssertionError('probe')):
            self.assertEqual(self.post(machine='box-c')[0], 200)
            self.project['defaultMachine'] = 'box-b'
            self.assertEqual(self.post()[0], 200)
            self.project['defaultMachine'] = None
            self.on_box('TASK-7')
            self.assertEqual(self.post()[0], 200)
            self.world.workers['box-b']['worktrees'].clear()
            with mock.patch.object(spawn, '_validate_and_check_session',
                                   side_effect=spawn.SpawnError('hub selected', status=418)):
                self.project['defaultMachineExplicit'] = True
                self.assertEqual(self.post()[0], 418)
                self.project['defaultMachineExplicit'] = False
                self.project['machines'] = []
                self.assertEqual(self.post()[0], 418)

    def test_unreachable_and_worker_check_failure_refuse_with_each_reason(self):
        self.figures['box-b']['reachable'] = False
        self.project['workerCheck'] = {'command': 'check.sh', 'doc': 'setup.md'}
        with mock.patch.object(remote, 'prepare_worker_readiness', return_value=(False, 'missing compiler')), \
             mock.patch.object(remote, 'check_hub_readiness', return_value=(False, 'missing hub tool')):
            status, body = self.post()
        self.assertEqual(status, 409, body)
        for part in ('box-b skipped: unreachable', 'box-c skipped: missing compiler',
                     'hub skipped: missing hub tool', 'setup.md'):
            self.assertIn(part, body['error'])
        self.assertEqual(self.launched, [])

    def test_hub_choice_includes_reason_and_unknown_worker_can_win(self):
        self.figures['hub']['ramAvailable'] = 8e9
        with mock.patch.object(spawn, '_ensure_worktree', return_value=self.repo):
            status, body = self.post()
        self.assertEqual(status, 200, body)
        self.assertEqual(body['machine'], 'hub')
        self.assertIn('placed on hub:', body['placementReason'])
        self.figures['hub']['ramAvailable'] = None
        self.figures['box-b']['ramAvailable'] = None
        self.figures['box-c']['ramAvailable'] = None
        self.project['workerCheck'] = {'command': 'check.sh', 'doc': 'setup.md'}
        with mock.patch.object(remote, 'prepare_worker_readiness', return_value=(True, 'passed')), \
             mock.patch.object(remote, 'check_hub_readiness', return_value=(False, 'failed')):
            status, body = self.post()
        self.assertEqual(status, 200, body)
        self.assertEqual(body['machine'], 'box-c')
        self.assertIn('free RAM unknown', body['placementReason'])

    def test_invalid_agent_and_done_task_are_refused_before_readiness_side_effects(self):
        self.project['workerCheck'] = {'command': 'check.sh', 'doc': 'setup.md'}
        with mock.patch.object(remote, 'prepare_worker_readiness', side_effect=AssertionError('readiness')), \
             mock.patch.object(remote, 'check_hub_readiness', side_effect=AssertionError('readiness')):
            self.assertEqual(self.post(agent='missing-agent')[0], 400)
            with mock.patch.object(spawn, 'spawn_cmd_override', return_value=None), \
                 mock.patch.object(server, 'run_backlog', return_value={'task': {'status': 'Done'}}):
                self.assertEqual(self.post()[0], 409)



class ExplicitHubDefaultTests(unittest.TestCase):
    def test_explicit_hub_survives_load_save_reload_and_null_restores_auto(self):
        import settings
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, 'projects.json')
            raw = {'machines': [BOX], 'projects': [
                {'name': 'app', 'path': directory, 'machines': ['box-b'], 'defaultMachine': 'hub'}]}
            with open(path, 'w') as stream:
                json.dump(raw, stream)
            config = server.load_config(path)
            self.assertTrue(config['projects'][0].get('defaultMachineExplicit'))
            with mock.patch.object(server, 'which', return_value='/fake/tool'):
                settings.apply_settings(config, {'defaultMachine': {'app': 'hub'}}, path=path)
            self.assertTrue(config['projects'][0]['defaultMachineExplicit'])
            self.assertTrue(server.load_config(path)['projects'][0].get('defaultMachineExplicit'))
            with open(path) as stream:
                self.assertEqual(json.load(stream)['projects'][0]['defaultMachine'], 'hub')
            with mock.patch.object(server, 'which', return_value='/fake/tool'):
                settings.apply_settings(config, {'defaultMachine': {'app': None}}, path=path)
            self.assertFalse(config['projects'][0]['defaultMachineExplicit'])
            self.assertFalse(server.load_config(path)['projects'][0].get('defaultMachineExplicit'))

    def test_settings_get_save_retains_auto_and_later_workers_retain_explicit_hub(self):
        import settings
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, 'projects.json')
            raw = {'machines': [BOX], 'projects': [
                {'name': 'app', 'path': directory, 'machines': ['box-b']},
                {'name': 'solo', 'path': directory}]}
            with open(path, 'w') as stream:
                json.dump(raw, stream)
            config = server.load_config(path)
            with mock.patch.object(server, 'which', return_value='/fake/tool'):
                defaults = settings.current_settings(config)['defaultMachine']
                self.assertEqual(defaults, {'app': None, 'solo': None})
                settings.apply_settings(config, {'defaultMachine': defaults}, path=path)
                self.assertFalse(config['projects'][0].get('defaultMachineExplicit'))
                self.assertIsNone(settings.current_settings(config)['defaultMachine']['app'])
                settings.apply_settings(config, {'defaultMachine': {'solo': 'hub'}}, path=path)
                settings.apply_settings(config, {'projectMachines': {'solo': ['box-b']}}, path=path)
                self.assertTrue(config['projects'][1]['defaultMachineExplicit'])
                self.assertEqual(settings.current_settings(config)['defaultMachine']['solo'], 'hub')
            reloaded = server.load_config(path)
            self.assertFalse(reloaded['projects'][0].get('defaultMachineExplicit'))
            self.assertTrue(reloaded['projects'][1]['defaultMachineExplicit'])


if __name__ == "__main__":
    unittest.main()
