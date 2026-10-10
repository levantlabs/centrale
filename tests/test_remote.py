"""Life of a remote task (doc-3, task-207): spawn, resume, End session and
cleanup on a machine, the task/* fetch back, and owed teardowns.

Hermetic: machines.run_batch is replaced by FakeWorker, which recognises
each step by the script or command it is sent and answers from its own
state, and remote.run_hub_git by a recorder. No ssh, git, tmux or agent
runs here; the worker scripts themselves run for real in
tests_integration/test_remote_integration.py."""

import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import machines  # noqa: E402
import remote  # noqa: E402
import server  # noqa: E402
import spawn  # noqa: E402
from machines import ANSWERED, UNKNOWN, UNREACHED, MachineResult  # noqa: E402

BOX = {"name": "box-b", "ssh": "user@box-b", "repoRoot": "~/centrale-repos",
       "worktreeRoot": "~/centrale-work", "maxAgents": 3}
HOME = "/home/user"
WT = f"{HOME}/centrale-work/app-task-7"


def _config(**project_extra):
    project = {"name": "app", "path": "/hub/app", "machines": ["box-b"], "defaultMachine": "box-b",
               "maxAgents": 2, "worktreeLinks": [], **project_extra}
    return {"machines": [dict(BOX)], "projects": [project],
            "agents": {"claude": {"cmd": ["claude"], "promptSuffix": None, "resumeCmd": None},
                       "codex": {"cmd": ["codex"], "promptSuffix": None, "resumeCmd": None}},
            "defaultAgent": "claude", "refreshIntervalSeconds": 10, "port": 7420}


def _placed(config):
    """The config's project as a task on box-b sees it (machines.placed)."""
    return machines.placed(config["projects"][0], "box-b")


def ok(stdout="", rc=0, stderr=""):
    return MachineResult(ANSWERED, rc, stdout, stderr, "box-b")


class FakeWorker:
    """Answers each run_batch call by the step it recognises."""

    def __init__(self):
        self.sessions = []          # names of live centrale-* sessions
        self.checkouts = {}         # branch -> path
        self.tip = None             # task branch tip in the bare repo
        self.worktree = "created-branch"
        self.paths = {"backlog": f"{HOME}/.npm-global/bin/backlog",
                      "claude": f"{HOME}/.npm-global/bin/claude",
                      "codex": f"{HOME}/.codex/bin/codex"}
        self.codex_help = "--dangerously-bypass-hook-trust"
        self.launch = ok()
        self.end = None
        self.cleanup = ok("centrale-killed \ncentrale-status-begin\n?? notes.txt\ncentrale-status-end\n"
                          "centrale-removed\ncentrale-branch-deleted\n")
        self.unreachable = False
        self.calls = []             # (step, argv)

    def step(self, argv):
        if argv[:2] == ["sh", "-c"]:
            script = argv[2]
            for step, known in (("sessions", remote._SESSIONS_SCRIPT), ("home", remote._HOME_SCRIPT),
                                ("checkouts", machines._WORKTREE_LIST), ("branches", machines._BRANCH_LIST),
                                ("worktree", remote._WORKTREE_SCRIPT),
                                ("rollback", remote._ROLLBACK_SCRIPT), ("end", remote._END_SCRIPT),
                                ("cleanup", remote._CLEANUP_SCRIPT)):
                if script == known:
                    return step
        if argv[:2] == ["bash", "-ic"]:
            return "resolve"
        if argv[:2] == ["sh", "-c"] and argv[2] == 'printf "%s\\n" "$PATH"':
            return "login-path"
        if len(argv) == 2 and argv[1] == "--help":
            return "help"
        if argv[0] == "tmux" and argv[1] == "new-session":
            return "launch"
        if argv[0] == "tmux" and argv[1] == "capture-pane":
            return "capture"
        if argv[:2] == ["git", "--git-dir"]:
            return "tip"
        if argv[:2] == ["python3", "-c"]:
            return "codex-lookup"
        raise AssertionError(f"unexpected worker command {argv!r}")

    def answer(self, step, argv):
        if step == "sessions":
            return ok("".join(f"{n}\t100\n" for n in self.sessions))
        if step == "home":
            return ok(HOME)
        if step == "checkouts":
            return ok("".join(f"worktree {p}\nHEAD abc\nbranch refs/heads/{b}\n\n"
                              for b, p in self.checkouts.items()))
        if step == "branches":
            return ok("refs/heads/task/task-7\n" if self.tip else "")
        if step == "worktree":
            return ok(f"centrale-worktree {self.worktree}\n")
        if step == "rollback":
            return ok()
        if step == "resolve":
            exe = argv[2].split()[-1]
            return ok(f"noise from .profile\n{self.paths[exe]}\n") if exe in self.paths else ok(rc=1)
        if step == "login-path":
            return ok("/usr/local/bin:/usr/bin:/bin\n")
        if step == "help":
            return ok(self.codex_help)
        if step == "launch":
            return self.launch
        if step == "capture":
            return ok("› \n  ? for shortcuts\n")
        if step == "tip":
            return ok(self.tip + "\n") if self.tip else ok(rc=1)
        if step == "codex-lookup":
            return ok(f"{HOME}/.codex\n11111111-2222-3333-4444-555555555555\n")
        if step == "end":
            return self.end or ok("centrale-ended @3 @9\ncentrale-swept 4242 4243\n")
        if step == "cleanup":
            return self.cleanup
        raise AssertionError(step)

    def run_batch(self, config, machine_name, commands, timeout=None, input=None):
        assert machine_name == "box-b"
        results = []
        for argv, _cwd in commands:
            argv = [str(a) for a in argv]
            step = self.step(argv)
            self.calls.append((step, argv))
            if self.unreachable:
                results.append(MachineResult(UNREACHED, None, "", "ssh: connect timed out", "box-b"))
            else:
                results.append(self.answer(step, argv))
        return results

    def steps(self):
        return [s for s, _ in self.calls]

    def argv(self, step):
        return next(a for s, a in self.calls if s == step)


class RemoteTestCase(unittest.TestCase):
    def setUp(self):
        self.worker = FakeWorker()
        self.hub_sessions = []
        self.hub_branches = set()   # task/<id> branches the hub has locally
        self.hub_git = []
        self.hub_git_rc = 0
        tmp = tempfile.mkdtemp(prefix="c207-")
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        self.owed_file = os.path.join(tmp, "owed.json")
        self.order = []
        for target, value in (
            (mock.patch.object(machines, "run_batch", self.worker.run_batch), None),
            (mock.patch.object(remote, "run_hub_git", self.fake_hub_git), None),
            (mock.patch.dict(os.environ, {remote.OWED_ENV: self.owed_file}), None),
            (mock.patch.object(spawn, "_check_not_done", lambda project, task_id: {"status": "To Do",
                                                                                 "assignees": ["@claude"]}), None),
            (mock.patch.object(spawn, "_claim_for_spawn", self.fake_claim), None),
            (mock.patch.object(spawn, "current_branch", lambda cwd: "main"), None),
            (mock.patch.object(remote, "_worker_hooks", lambda cmd, supports_hooks=None:
                               ["--hooks", "tmux", str(supports_hooks)]), None),
            (mock.patch.object(server, "record_session_started", lambda *a, **k: None), None),
            (mock.patch.object(server, "record_session_ended", lambda *a, **k: None), None),
            (mock.patch.object(server, "run_git", self.fake_run_git), None),
            (mock.patch.object(server, "list_sessions", lambda *a, **k: list(self.hub_sessions)), None),
            (mock.patch.object(server, "_read_main_task_status", lambda repo, task: "Done"), None),
            (mock.patch.object(spawn, "unlock_task_file", lambda repo, task: None), None),
        ):
            target.start()
            self.addCleanup(target.stop)
        os.environ.pop("CENTRALE_SPAWN_CMD", None)
        server._reset_agent_events()
        remote._fetches.clear()

    def fake_claim(self, config, project, task_id, recorded, warning):
        self.order.append("claim")
        return []

    def fake_hub_git(self, args, cwd, timeout=None):
        self.hub_git.append(args)
        self.order.append(args[0])
        return server.subprocess.CompletedProcess(args, self.hub_git_rc, "", "push refused" if self.hub_git_rc else "")

    def fake_run_git(self, args, cwd=None):
        # task-224: the hub's own evidence of a task (machines.locate).
        if args[:1] == ["rev-parse"] and args[-1].startswith("refs/heads/task/"):
            found = args[-1][len("refs/heads/"):] in self.hub_branches
            return server.subprocess.CompletedProcess(args, 0 if found else 1, "f" * 40 + "\n" if found else "", "")
        if args[:1] == ["rev-parse"]:
            return server.subprocess.CompletedProcess(args, 0, "f" * 40 + "\n", "")
        return server.subprocess.CompletedProcess(args, 0, "", "")


class SpawnOnMachineTests(RemoteTestCase):
    def test_first_message_is_in_the_worker_launch_prompt(self):
        message = "Plan and wait for my ruling before making changes."
        spawn.spawn(_config(), "app", "TASK-7", first_message=message)
        launch = self.worker.argv("launch")
        prompts = [arg for arg in launch if message in arg]
        self.assertEqual(len(prompts), 1)
        self.assertEqual(prompts[0], spawn.prompt_for("TASK-7") + "\n\n" +
                         "First message from the orchestrator for this spawn "
                         "(follow it before starting task work):\n" + message)

    def test_claims_on_hub_then_pushes_main_then_launches_by_full_path(self):
        result = spawn.spawn(_config(), "app", "TASK-7")
        self.assertEqual(self.order[:2], ["claim", "push"])
        self.assertEqual(self.hub_git[0][-2:], ["user@box-b:centrale-repos/app/.git", "refs/heads/main:refs/heads/main"])
        self.assertEqual(self.worker.steps(), ["checkouts", "branches",  # where the task is (locate)
                                               "sessions", "home", "checkouts", "worktree", "resolve",
                                               "login-path", "resolve", "resolve", "resolve", "launch"])
        worktree = self.worker.argv("worktree")
        self.assertEqual(worktree[4:8], ["~/centrale-repos/app/.git", "~/centrale-work/app-task-7",
                                         "task/task-7", "main"])
        launch = self.worker.argv("launch")
        self.assertIn(f"{HOME}/.npm-global/bin/claude", launch)
        self.assertNotIn("claude", launch[:launch.index(f"{HOME}/.npm-global/bin/claude")])
        self.assertIn("--hooks", launch)
        self.assertIn(spawn.prompt_for("TASK-7"), launch)
        self.assertFalse(any(a.startswith("CENTRALE_EVENT_URL=") for a in launch))
        self.assertIn(f"CENTRALE_SPAWN_ID={result['spawnId']}", launch)
        self.assertEqual(launch[launch.index("@centrale_spawn") + 1], result["spawnId"])
        self.assertEqual(result["machine"], "box-b")
        self.assertEqual(result["worktree"], "~/centrale-work/app-task-7")
        self.assertIn("ssh -t user@box-b tmux attach -t centrale-app-task-7", result["attach"])

    def test_identity_options_ride_in_the_launch_call_and_rearm_follows(self):
        import types
        fake = types.ModuleType("remote_status")
        rearmed = []
        fake.identity_commands = lambda target, project, task, kind: [
            ["set-option", "-p", "-t", target, "@centrale_task", task],
            ["tmux", "set-option", "-p", "-t", target, "@centrale_kind", kind]]
        fake.rearm = lambda config, machine: rearmed.append(machine)
        with mock.patch.dict(sys.modules, {"remote_status": fake}):
            spawn.spawn(_config(), "app", "TASK-7")
        launch = self.worker.argv("launch")
        tail = launch[-14:]
        self.assertEqual(tail, [";", "set-option", "-p", "-t", "=centrale-app-task-7:", "@centrale_task", "TASK-7",
                                ";", "set-option", "-p", "-t", "=centrale-app-task-7:", "@centrale_kind", "claude"])
        self.assertEqual(self.worker.steps().count("launch"), 1)
        self.assertEqual(rearmed, ["box-b"])

    def test_worktree_links_are_sent_with_their_worker_sources(self):
        spawn.spawn(_config(worktreeLinks=[".venv"]), "app", "TASK-7")
        self.assertEqual(self.worker.argv("worktree")[-2:], [".venv", "~/centrale-repos/app/.venv"])

    def test_codex_hook_trust_is_probed_on_the_worker(self):
        spawn.spawn(_config(), "app", "TASK-7", agent="codex")
        self.assertEqual(self.worker.argv("help"), [f"{HOME}/.codex/bin/codex", "--help"])
        launch = self.worker.argv("launch")
        self.assertIn("True", launch)
        self.assertFalse(any("trust_level" in arg for arg in launch))
        self.worker.calls.clear()
        self.worker.sessions = []
        self.worker.codex_help = "usage: codex"
        spawn.spawn(_config(), "app", "TASK-8", agent="codex")
        self.assertIn("False", self.worker.argv("launch"))

    def test_worker_session_path_contains_all_interactively_resolved_tools(self):
        spawn.spawn(_config(), "app", "TASK-7")
        launch = self.worker.argv("launch")
        self.assertIn("PATH=/usr/local/bin:/usr/bin:/bin:/home/user/.npm-global/bin:/home/user/.codex/bin",
                      launch)

    def test_interrupted_launch_reports_outcome_unknown_and_never_starts_a_second_agent(self):
        self.worker.launch = MachineResult(UNKNOWN, None, "", "connection closed", "box-b")
        with self.assertRaises(spawn.SpawnError) as caught:
            spawn.spawn(_config(), "app", "TASK-7")
        self.assertEqual(caught.exception.status, 504)
        self.assertIn("outcome unknown", caught.exception.message)
        self.assertEqual(self.worker.steps().count("launch"), 1)
        self.assertNotIn("rollback", self.worker.steps())
        # It did start: the retry sees the session and refuses before any side effect.
        self.worker.sessions = ["centrale-app-task-7"]
        self.worker.calls.clear()
        self.order.clear()
        with self.assertRaises(spawn.SpawnError) as caught:
            spawn.spawn(_config(), "app", "TASK-7")
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(self.worker.steps(), ["checkouts", "branches", "sessions", "home", "checkouts"])
        self.assertEqual(self.order, [])

    def test_failed_launch_rolls_back_what_this_spawn_created_and_names_machine_and_step(self):
        self.worker.launch = ok(rc=1, stderr="duplicate session")
        with self.assertRaises(spawn.SpawnError) as caught:
            spawn.spawn(_config(), "app", "TASK-7")
        self.assertIn("machine box-b", caught.exception.message)
        self.assertIn("start the agent session", caught.exception.message)
        self.assertEqual(self.worker.argv("rollback")[-1], "branch")

    def test_reused_worktree_is_never_rolled_back(self):
        self.worker.worktree = "reused"
        self.worker.launch = ok(rc=1, stderr="boom")
        with self.assertRaises(spawn.SpawnError):
            spawn.spawn(_config(), "app", "TASK-7")
        self.assertNotIn("rollback", self.worker.steps())

    def test_unreachable_worker_refuses_before_claiming(self):
        self.worker.unreachable = True
        with self.assertRaises(spawn.SpawnError) as caught:
            spawn.spawn(_config(), "app", "TASK-7")
        self.assertEqual(caught.exception.status, 503)
        self.assertIn("machine box-b is unreachable", caught.exception.message)
        self.assertEqual(self.order, [])

    def test_failed_push_names_machine_and_step(self):
        self.hub_git_rc = 1
        with self.assertRaises(spawn.SpawnError) as caught:
            spawn.spawn(_config(), "app", "TASK-7")
        self.assertIn("machine box-b: pushing main", caught.exception.message)
        self.assertNotIn("worktree", self.worker.steps())

    def test_project_and_machine_caps_count_the_workers_sessions(self):
        self.worker.sessions = ["centrale-app-task-1", "centrale-app-task-2"]
        with self.assertRaises(spawn.SpawnError) as caught:
            spawn.spawn(_config(), "app", "TASK-7")
        self.assertIn("maxAgents: 2", caught.exception.message)
        self.worker.sessions = ["centrale-app-task-1", "centrale-app-task-2", "centrale-app-task-3"]
        with self.assertRaises(spawn.SpawnError) as caught:
            spawn.spawn(_config(maxAgents=None), "app", "TASK-7")
        self.assertIn("Machine box-b has 3", caught.exception.message)

    def test_branch_checked_out_elsewhere_on_the_worker_is_refused(self):
        self.worker.checkouts = {"task/task-7": f"{HOME}/elsewhere"}
        with self.assertRaises(spawn.SpawnError) as caught:
            spawn.spawn(_config(), "app", "TASK-7")
        self.assertEqual(caught.exception.status, 409)
        self.assertIn("box-b:/home/user/elsewhere", caught.exception.message)
        self.worker.checkouts = {"task/task-7": WT}
        spawn.spawn(_config(), "app", "TASK-7")

    def test_missing_cli_on_the_worker_rolls_back_and_says_where(self):
        self.worker.paths = {}
        with self.assertRaises(spawn.SpawnError) as caught:
            spawn.spawn(_config(), "app", "TASK-7")
        self.assertIn("claude not found", caught.exception.message)
        self.assertIn("rollback", self.worker.steps())
        self.assertNotIn("launch", self.worker.steps())


class ResumeOnMachineTests(RemoteTestCase):
    def test_codex_resume_uses_the_workers_conversation_store(self):
        self.worker.worktree = "reused"
        config = _config()
        with mock.patch.object(spawn, "_check_not_done", lambda p, t: {"status": "In Progress",
                                                                        "assignees": ["@codex"]}):
            result = spawn.resume(config, "app", "TASK-7")
        lookup = self.worker.argv("codex-lookup")
        self.assertEqual(lookup[-1], "~/centrale-work/app-task-7")
        launch = self.worker.argv("launch")
        self.assertIn(f"CODEX_HOME={HOME}/.codex", launch)
        i = launch.index(f"{HOME}/.codex/bin/codex")
        self.assertEqual(launch[i + 1:i + 3], ["resume", "11111111-2222-3333-4444-555555555555"])
        self.assertFalse(any("trust_level" in arg for arg in launch))
        self.assertIn("PATH=/usr/local/bin:/usr/bin:/bin:/home/user/.npm-global/bin:/home/user/.codex/bin",
                      launch)
        self.assertEqual(result["conversationStatus"], "resumed")
        self.assertTrue(result["resumed"])
        self.assertEqual(self.order, ["push"])  # resume never claims again

    def test_claude_resume_continues_by_full_path(self):
        self.worker.worktree = "reused"
        spawn.resume(_config(), "app", "TASK-7")
        launch = self.worker.argv("launch")
        i = launch.index(f"{HOME}/.npm-global/bin/claude")
        self.assertEqual(launch[i + 1], "--continue")


class EndSessionTests(RemoteTestCase):
    def test_kills_session_linked_windows_and_sweeps_on_the_worker(self):
        status, body = remote.end_session(_config(), _placed(_config()), "TASK-7")
        self.assertEqual(status, 200)
        self.assertEqual(body["killedWindows"], ["@3", "@9"])
        self.assertEqual(body["killedOrphans"], [4242, 4243])
        argv = self.worker.argv("end")
        self.assertEqual(argv[4:8], ["centrale-app-task-7", "", "~/centrale-work/app-task-7", "strict"])
        self.assertEqual(argv[8], remote.SWEEP_PROGRAM)
        self.assertEqual(argv[9:], ["tmux"])

    def test_no_session_is_404(self):
        self.worker.end = ok("centrale-no-session\n", rc=3)
        status, body = remote.end_session(_config(), _placed(_config()), "TASK-7")
        self.assertEqual(status, 404)

    def test_unreachable_teardown_is_owed_then_retried_when_it_returns(self):
        config = _config()
        self.worker.unreachable = True
        status, body = remote.end_session(config, _placed(config), "TASK-7")
        self.assertEqual(status, 202)
        self.assertTrue(body["owed"])
        owed = remote.owed_teardowns()
        self.assertEqual([(e["kind"], e["taskId"]) for e in owed], [("end-session", "TASK-7")])
        # Still down: still owed.
        outcomes = remote.settle_owed(config, "box-b")
        self.assertIn("still owed", outcomes[0][1])
        self.assertEqual(len(remote.owed_teardowns()), 1)
        # Back: retried as a lenient end bounded by the request time, then dropped.
        self.worker.unreachable = False
        self.worker.calls.clear()
        outcomes = remote.settle_owed(config, "box-b")
        self.assertEqual(outcomes[0][1], "done")
        argv = self.worker.argv("end")
        self.assertEqual(argv[7], "lenient")
        self.assertLessEqual(abs(int(argv[5]) - int(owed[0]["requestedAt"] + 2)), 1)
        self.assertEqual(remote.owed_teardowns(), [])
        self.assertFalse(os.path.exists(self.owed_file))

    def test_owed_end_leaves_a_newer_session_alone(self):
        config = _config()
        remote.record_owed({"kind": "end-session", "project": "app", "taskId": "TASK-7",
                            "machine": "box-b", "requestedAt": 1000.0})
        self.worker.end = ok("centrale-newer 2000\n", rc=5)
        outcomes = remote.settle_owed(config, "box-b")
        self.assertIn("newer than the request", outcomes[0][1])
        self.assertEqual(remote.owed_teardowns(), [])

    def test_owed_teardown_settles_before_a_spawn_of_that_task(self):
        config = _config()
        remote.record_owed({"kind": "end-session", "project": "app", "taskId": "TASK-7",
                            "machine": "box-b", "requestedAt": 1000.0})
        spawn.spawn(config, "app", "TASK-7")
        steps = self.worker.steps()
        self.assertLess(steps.index("end"), steps.index("sessions"))
        self.assertEqual(remote.owed_teardowns(), [])

    def test_background_settle_skips_a_busy_task(self):
        config = _config()
        remote.record_owed({"kind": "end-session", "project": "app", "taskId": "TASK-7",
                            "machine": "box-b", "requestedAt": 1000.0})
        with server.task_lifecycle_lock("app", "TASK-7"):
            outcomes = remote.settle_owed(config, "box-b")
        self.assertIn("busy", outcomes[0][1])
        self.assertEqual(len(remote.owed_teardowns()), 1)


class CleanupTests(RemoteTestCase):
    TIP = "f" * 40

    def test_dirty_worker_refuses_with_dequoted_paths(self):
        self.worker.tip = self.TIP
        self.worker.checkouts = {"task/task-7": WT}
        self.worker.cleanup = ok('centrale-status-begin\n M "notes with spaces.txt"\ncentrale-status-end\n', rc=12)
        status, body = remote.cleanup(_config(), _placed(_config()), "TASK-7")
        self.assertEqual(status, 409)
        self.assertEqual(body["discardedPaths"], ["notes with spaces.txt"])
        self.assertNotIn("centrale-removed", self.worker.cleanup.stdout)

    def test_owed_cleanup_drops_when_worker_became_dirty(self):
        self.worker.cleanup = ok("centrale-status-begin\n?? evidence.log\ncentrale-status-end\n", rc=12)
        remote.record_owed({"kind": "cleanup", "project": "app", "taskId": "TASK-7",
                           "machine": "box-b", "requestedAt": 1000.0,
                           "expectedTip": self.TIP, "force": False})
        outcomes = remote.settle_owed(_config(), "box-b")
        self.assertIn("evidence.log", outcomes[0][1])
        self.assertEqual(remote.owed_teardowns(), [])

    def test_fetches_verifies_on_hub_and_removes_on_worker(self):
        self.worker.tip = self.TIP
        self.worker.checkouts = {"task/task-7": WT}
        status, body = remote.cleanup(_config(), _placed(_config()), "TASK-7", force=True)
        self.assertEqual(status, 200, body)
        self.assertEqual(self.hub_git[0][-1], "+refs/heads/task/task-7:refs/remotes/box-b/task/task-7")
        argv = self.worker.argv("cleanup")
        self.assertEqual(argv[7], self.TIP)
        self.assertEqual(argv[8], "1")
        self.assertEqual(body["discardedPaths"], ["notes.txt"])
        self.assertTrue(body["worktreeRemoved"])

    def test_live_session_refuses(self):
        self.worker.tip = self.TIP
        self.worker.sessions = ["centrale-app-task-7"]
        status, body = remote.cleanup(_config(), _placed(_config()), "TASK-7")
        self.assertEqual(status, 409)
        self.assertNotIn("cleanup", self.worker.steps())

    def test_unmerged_branch_refuses(self):
        self.worker.tip = "a" * 40  # the hub verified a different tip
        status, body = remote.cleanup(_config(), _placed(_config()), "TASK-7")
        self.assertEqual(status, 409)
        self.assertIn("not fully merged", body["error"])

    def test_unreachable_with_a_merged_hub_copy_is_owed_with_that_tip(self):
        self.worker.unreachable = True
        status, body = remote.cleanup(_config(), _placed(_config()), "TASK-7")
        self.assertEqual(status, 202)
        self.assertEqual(remote.owed_teardowns()[0]["expectedTip"], self.TIP)
        self.worker.unreachable = False
        outcomes = remote.settle_owed(_config(), "box-b")
        self.assertEqual(outcomes[0][1], "done")
        self.assertEqual(self.worker.argv("cleanup")[7], self.TIP)

    def test_unreachable_without_a_merged_hub_copy_records_nothing(self):
        self.worker.unreachable = True
        with mock.patch.object(server, "_read_main_task_status", lambda repo, task: "In Progress"):
            status, body = remote.cleanup(_config(), _placed(_config()), "TASK-7")
        self.assertEqual(status, 503)
        self.assertEqual(remote.owed_teardowns(), [])


class FetchTests(RemoteTestCase):
    def test_refresh_fetches_task_branches_from_reachable_machines_into_remote_refs(self):
        config = _config()
        report = [{"name": "box-b", "reach": "reachable", "error": None, "results": []}]
        remote.after_refresh(config, report)
        self.assertEqual(self.hub_git, [["fetch", "--quiet", "--prune", "--no-write-fetch-head",
                                         "user@box-b:centrale-repos/app/.git",
                                         "+refs/heads/task/*:refs/remotes/box-b/task/*"]])
        status = remote.fetch_status(config["projects"][0], "box-b")
        self.assertEqual(status["fetchAgeSeconds"], 0)
        self.assertIsNone(status["error"])

    def test_unreachable_machine_is_not_fetched_and_age_stays_unknown(self):
        config = _config()
        remote.after_refresh(config, [{"name": "box-b", "reach": "unreachable", "error": "x", "results": []}])
        self.assertEqual(self.hub_git, [])
        self.assertIsNone(remote.fetch_status(config["projects"][0], "box-b")["fetchAgeSeconds"])

    def test_failed_fetch_keeps_the_last_age_and_reports_the_error(self):
        config = _config()
        project = config["projects"][0]
        remote.fetch_task_branches(config, BOX, project)
        self.hub_git_rc = 1
        remote.fetch_task_branches(config, BOX, project)
        status = remote.fetch_status(project, "box-b", now=remote._fetches[("app", "box-b")]["at"] + 30)
        self.assertEqual(status["fetchAgeSeconds"], 30)
        self.assertEqual(status["error"], "push refused")

    def test_board_carries_fetch_age_and_owed_only_with_machines(self):
        config = _config()
        with mock.patch.object(server, "_load_project_board", lambda c, p, r=None: {"name": p["name"], "tasks": []}), \
                mock.patch.object(machines, "refresh", lambda c, reads=None: [
                    {"name": "box-b", "reach": "reachable", "error": None, "results": []}]):
            board = server.get_board(config)
        self.assertEqual([f["machine"] for f in board["projects"][0]["fetches"]], ["box-b"])
        self.assertEqual(board["projects"][0]["machines"], ["box-b"])
        self.assertEqual(board["owedTeardowns"], [])
        local = {"projects": [{"name": "app", "path": "/hub/app"}]}
        with mock.patch.object(server, "_load_project_board", lambda c, p, r=None: {"name": p["name"], "tasks": []}):
            board = server.get_board(local)
        self.assertNotIn("fetches", board["projects"][0])
        self.assertNotIn("owedTeardowns", board)


class GitUrlTests(unittest.TestCase):
    def test_worker_paths_become_urls_without_expanding_tilde_on_the_hub(self):
        self.assertEqual(remote.git_url(BOX, "~/r/app.git"), "user@box-b:r/app.git")
        self.assertEqual(remote.git_url(BOX, "/srv/app.git"), "user@box-b:/srv/app.git")
        self.assertEqual(remote.git_url({"ssh": "ssh://u@h:2222"}, "~/r.git"), "ssh://u@h:2222/~/r.git")


if __name__ == "__main__":
    unittest.main()

class WorkerReadinessTests(RemoteTestCase):
    def test_refuses_before_claim_and_creation(self):
        value = {'command': 'scripts/check.sh', 'doc': 'docs/setup.md'}
        for reason in ('failed: missing venv', 'timed out', 'setup not finished: scripts/check.sh missing'):
            with self.subTest(reason=reason), mock.patch.object(remote, 'prepare_worker_readiness', return_value=(False, reason)):
                with self.assertRaises(spawn.SpawnError) as caught:
                    remote.spawn_on_machine(_config(workerCheck=value), 'app', 'TASK-7', machine_name='box-b')
                self.assertNotIn('claim', self.order)
                self.assertNotIn('worktree', self.worker.steps())
                self.assertNotIn('launch', self.worker.steps())
                self.assertIn('read docs/setup.md in app and set up box-b, or spawn on the hub', str(caught.exception))
                self.assertIn(reason, str(caught.exception))

    def test_pass_continues_without_warning_and_no_check_bypasses(self):
        for declared in (False, True):
            self.order.clear()
            with mock.patch.object(remote, 'prepare_worker_readiness', return_value=(True, 'PASS')) as check:
                result = remote.spawn_on_machine(_config(**({'workerCheck': {'command': 'check.sh', 'doc': 'setup.md'}} if declared else {})), 'app', 'TASK-7', machine_name='box-b')
                self.assertEqual(check.call_count, int(declared))
                self.assertEqual(self.order[:2], ['claim', 'push'])
                self.assertEqual(result.get('warnings', []), [])

    def test_pinned_base_check_precedes_claim_then_claimed_base_is_pushed(self):
        project_check = {'command': 'check.sh', 'doc': 'setup.md'}
        def check(config, machine, project, sha):
            self.order.append('check')
            self.assertEqual(sha, 'f' * 40)
            return True, 'ready'
        with mock.patch.object(remote, 'check_worker_readiness', side_effect=check):
            remote.spawn_on_machine(_config(workerCheck=project_check), 'app', 'TASK-7', machine_name='box-b')
        self.assertEqual(self.order[:4], ['push', 'check', 'claim', 'push'])
        self.assertEqual(self.hub_git[0][-1], 'f' * 40 + ':refs/heads/main')
        self.assertEqual(self.hub_git[1][-1], 'refs/heads/main:refs/heads/main')

    def test_preflight_push_failure_cannot_claim(self):
        self.hub_git_rc = 1
        with self.assertRaises(spawn.SpawnError) as caught:
            remote.spawn_on_machine(_config(workerCheck={'command': 'check.sh', 'doc': 'setup.md'}),
                                    'app', 'TASK-7', machine_name='box-b')
        self.assertNotIn('claim', self.order)
        self.assertIn('pushing base', str(caught.exception))
        self.assertNotIn('worktree', self.worker.steps())

    def test_executor_bounds_output_and_names_failures(self):
        project = _config(workerCheck={'command': 'check.sh', 'doc': 'setup.md'})['projects'][0]
        for rc, expected in ((0, None), (1, 'failed'), (124, 'timed out'), (126, 'failed'), (127, 'failed'), (31, 'setup not finished'), (30, 'cleanup')):
            with self.subTest(rc=rc), mock.patch.object(machines, 'run_on_machine', return_value=ok('x' * 5000, rc)) as run:
                passed, reason = remote.check_worker_readiness({}, BOX, project, 'f' * 40)
                self.assertEqual(passed, rc == 0)
                if expected:
                    self.assertIn(expected, reason)
                    self.assertLess(len(reason), 2200)
                self.assertGreater(run.call_args.kwargs['timeout'], remote.WORKER_CHECK_SECONDS)

    def test_doctor_never_pushes_and_absent_main_warns(self):
        project = _config(workerCheck={'command': 'check.sh', 'doc': 'setup.md'})['projects'][0]
        with mock.patch.object(machines, 'run_on_machine', return_value=ok('', 1)), mock.patch.object(remote, 'run_hub_git') as push:
            level, reason = remote.doctor_worker_readiness({}, BOX, project)
            self.assertEqual(level, 'WARN')
            self.assertIn('not pushed yet; checked at the next spawn', reason)
            push.assert_not_called()

    def test_doctor_existing_base_and_behind(self):
        project = _config(workerCheck={'command': 'check.sh', 'doc': 'setup.md'})['projects'][0]
        with mock.patch.object(machines, 'run_on_machine', return_value=ok('e' * 40)), mock.patch.object(remote, 'check_worker_readiness', return_value=(True, 'ready')) as check, mock.patch.object(remote, 'run_hub_git') as push:
            level, reason = remote.doctor_worker_readiness({}, BOX, project)
            self.assertEqual(level, 'PASS')
            self.assertIn('behind hub main', reason)
            self.assertEqual(check.call_args.args[-1], 'e' * 40)
            push.assert_not_called()


    def test_doctor_uses_non_main_base_on_worker_and_hub(self):
        project = _config(workerCheck={'command': 'check.sh', 'doc': 'setup.md'})['projects'][0]
        with mock.patch.object(spawn, 'current_branch', return_value='master'), \
             mock.patch.object(machines, 'run_on_machine', return_value=ok('e' * 40)) as read, \
             mock.patch.object(remote, 'check_worker_readiness', return_value=(True, 'ready')), \
             mock.patch.object(server, 'run_git', side_effect=self.fake_run_git) as git, \
             mock.patch.object(remote, 'run_hub_git') as push:
            level, reason = remote.doctor_worker_readiness({}, BOX, project)
        self.assertEqual(level, 'PASS')
        self.assertEqual(read.call_args.args[2][-1], 'refs/heads/master')
        self.assertEqual(git.call_args_list[0].args[0][-1], 'refs/heads/master')
        self.assertEqual(git.call_args_list[1].args[0][-1], 'refs/heads/master')
        self.assertIn('behind hub master', reason)
        push.assert_not_called()

    def test_sweep_without_proc_exits_successfully_without_enumerating(self):
        with mock.patch.object(os.path, 'isdir', return_value=False), \
             mock.patch.object(os, 'listdir', side_effect=AssertionError('no proc enumeration')), \
             mock.patch.object(sys, 'argv', ['sweep', '/isolated/scratch', 'kill']):
            with self.assertRaises(SystemExit) as caught:
                exec(remote._WORKER_SWEEP_PROGRAM, {})
        self.assertEqual(caught.exception.code, 0)
