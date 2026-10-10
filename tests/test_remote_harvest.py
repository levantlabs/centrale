"""Merge gates, Discard and Abandon for a task on a machine (doc-3,
task-208).

Hermetic: machines.run_batch is replaced by a fake worker that recognises
each step by the script it is sent and answers from its own state;
remote.run_hub_git and server.run_git by fakes for the hub's git. No ssh,
git, tmux or agent runs here; the worker scripts run for real in
tests_integration/test_remote_integration.py."""

import contextlib
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import harvest  # noqa: E402
import machines  # noqa: E402
import remote  # noqa: E402
import remote_harvest  # noqa: E402
import server  # noqa: E402
import spawn  # noqa: E402
from machines import ANSWERED, UNKNOWN, UNREACHED, MachineResult  # noqa: E402

HOME = "/home/user"
WT = f"{HOME}/centrale-work/app-task-7"
TIP = "a" * 40
BASE = "b" * 40
REF = "refs/remotes/box-b/task/task-7"


def ok(stdout="", rc=0, stderr=""):
    return MachineResult(ANSWERED, rc, stdout, stderr, "box-b")


def _config(**extra):
    project = {"name": "app", "path": "/hub/app", "machines": ["box-b"], "defaultMachine": None,
               "maxAgents": None, "worktreeLinks": [], **extra}
    return {"machines": [{"name": "box-b", "ssh": "user@box-b", "repoRoot": "~/centrale-repos",
                          "worktreeRoot": "~/centrale-work", "maxAgents": None}],
            "projects": [project], "refreshIntervalSeconds": 10}


class FakeWorker:
    def __init__(self):
        self.sessions = []
        self.checkouts = {"task/task-7": WT}
        self.tip = TIP
        self.tips = None            # successive tips for later reads
        self.status = ""            # porcelain, or None for no worktree
        self.scratch = ok("centrale-merged\n")
        self.throwaway = ok("centrale-removed\ncentrale-branch-deleted\n")
        self.cleanup = ok("centrale-killed \ncentrale-removed\ncentrale-branch-deleted\n")
        self.unreachable = set()    # steps that do not answer
        self.calls = []

    def step(self, argv):
        known = {remote._SESSIONS_SCRIPT: "sessions", remote._HOME_SCRIPT: "home",
                 machines._WORKTREE_LIST: "checkouts", machines._BRANCH_LIST: "branches",
                 remote_harvest._TIP_SCRIPT: "tip",
                 remote_harvest._STATUS_SCRIPT: "status", remote_harvest._SCRATCH_SCRIPT: "scratch",
                 remote_harvest._THROWAWAY_SCRIPT: "throwaway", remote._CLEANUP_SCRIPT: "cleanup"}
        return known[argv[2]]

    def answer(self, step):
        if step == "sessions":
            return ok("".join(f"{n}\t100\n" for n in self.sessions))
        if step == "home":
            return ok(HOME)
        if step == "checkouts":
            return ok("".join(f"worktree {p}\nHEAD abc\nbranch refs/heads/{b}\n\n"
                              for b, p in self.checkouts.items()))
        if step == "branches":
            return ok("refs/heads/task/task-7\n" if self.tip else "")
        if step == "tip":
            tip = self.tips.pop(0) if self.tips else self.tip
            return ok(f"{tip}\n" if tip else "")
        if step == "status":
            return ok("centrale-no-worktree\n") if self.status is None else ok(self.status)
        return getattr(self, step)

    def run_batch(self, config, machine_name, commands, timeout=None, input=None):
        assert machine_name == "box-b"
        results = []
        for argv, _cwd in commands:
            argv = [str(a) for a in argv]
            step = self.step(argv)
            self.calls.append((step, argv, timeout))
            if step in self.unreachable or "all" in self.unreachable:
                results.append(MachineResult(UNREACHED, None, "", "ssh: connect timed out", "box-b"))
            else:
                results.append(self.answer(step))
        return results

    def steps(self):
        return [s for s, _a, _t in self.calls]

    def call(self, step):
        return next((a, t) for s, a, t in self.calls if s == step)


class RemoteHarvestCase(unittest.TestCase):
    def setUp(self):
        self.worker = FakeWorker()
        self.config = _config()
        # remote_harvest's functions take the project placed at the task's
        # location (task-224); harvest.harvest_branch derives it itself.
        self.project = machines.placed(self.config["projects"][0], "box-b")
        self.order = []
        self.hub = {"refs/heads/main": BASE, REF: TIP}
        self.fetch_brings = TIP
        self.task_status = "Done"
        tmp = tempfile.mkdtemp(prefix="c208-")
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        for patcher in (
            mock.patch.object(machines, "run_batch", self.worker.run_batch),
            mock.patch.object(remote, "run_hub_git", self.fake_hub_git),
            mock.patch.object(server, "run_git", self.fake_run_git),
            mock.patch.object(server, "run_backlog", self.fake_backlog),
            mock.patch.object(spawn, "current_branch", lambda cwd: "main"),
            mock.patch.object(spawn, "detached_snapshot", self.fake_snapshot),
            mock.patch.object(spawn, "unlock_task_file", lambda repo, task: True),
            mock.patch.dict(os.environ, {remote.OWED_ENV: os.path.join(tmp, "owed.json")}),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    @contextlib.contextmanager
    def fake_snapshot(self, repo, ref):
        self.order.append(f"snapshot {ref}")
        yield "/tmp/snapshot", None

    def fake_backlog(self, args, cwd=None):
        return {"task": {"id": "TASK-7", "title": "Remote task", "status": self.task_status,
                         "acceptanceCriteria": [], "path": "backlog/tasks/task-7 - Remote-task.md"}}

    def fake_hub_git(self, args, cwd, timeout=None):
        self.order.append(" ".join(args[:1] + args[-1:]))
        if args[0] == "fetch":
            self.hub[REF] = self.fetch_brings
        return subprocess.CompletedProcess(args, 0, "", "")

    def fake_run_git(self, args, cwd=None):
        self.order.append(" ".join(args))
        rc, out = 0, ""
        if args[:3] == ["rev-parse", "--verify", "--quiet"]:
            sha = self.hub.get(args[3])
            rc, out = (0, sha + "\n") if sha else (1, "")
        elif args[:2] == ["rev-list", "--count"]:
            out = "0\n" if args[2].startswith(TIP) else "2\n"
        elif args[:2] == ["diff", "--name-only"]:
            out = "work.txt\n"
        return subprocess.CompletedProcess(args, rc, out, "")

    def evaluate(self):
        return harvest.evaluate_branch(self.config, self.project, "TASK-7")

    def gate(self, report, name):
        return next(g for g in report["gates"] if g["name"] == name)


class GateTests(RemoteHarvestCase):
    def test_a_live_session_on_the_worker_blocks_the_merge(self):
        self.worker.sessions = ["centrale-app-task-7"]
        report = self.evaluate()
        self.assertFalse(report["harvestable"])
        self.assertEqual(report["machine"], "box-b")
        self.assertIn("live tmux session is still running on machine box-b", self.gate(report, "noLiveSession")["reason"])
        self.assertNotIn("scratch", self.worker.steps())

    def test_a_dirty_worktree_on_the_worker_blocks_the_merge(self):
        self.worker.status = " M src/app.py\n?? notes.txt\n"
        report = self.evaluate()
        gate = self.gate(report, "worktreeClean")
        self.assertFalse(gate["passed"])
        self.assertIn("worktree on machine box-b has uncommitted changes (2 files: src/app.py, notes.txt)",
                      gate["reason"])
        self.assertNotIn("scratch", self.worker.steps())

    def test_no_worktree_on_the_worker_reads_the_branch_and_says_so(self):
        self.worker.status = None
        self.worker.checkouts = {}
        report = self.evaluate()
        self.assertTrue(report["harvestable"], report)
        self.assertEqual(self.gate(report, "worktreeClean")["reason"],
                         "no worktree on machine box-b -- nothing uncommitted to protect")

    def test_external_checkout_is_detected_in_the_workers_bare_repository(self):
        self.worker.checkouts = {"task/task-7": f"{HOME}/elsewhere"}
        report = self.evaluate()
        gate = self.gate(report, "taskDone")
        self.assertFalse(gate["passed"])
        self.assertIn(f"checked out outside Centrale at box-b:{HOME}/elsewhere", gate["reason"])
        self.assertEqual(report["branchCheckout"]["kind"], "external")

    def test_task_done_is_read_from_the_pinned_fetched_commit(self):
        report = self.evaluate()
        self.assertTrue(report["harvestable"], report)
        self.assertIn(f"snapshot {TIP}", self.order)
        self.assertLess(self.order.index(f"fetch +refs/heads/task/task-7:{REF}"), self.order.index(f"snapshot {TIP}"))

    def test_a_fetch_that_brings_back_another_commit_is_refused(self):
        self.fetch_brings = "c" * 40
        report = self.evaluate()
        self.assertIn("moved on machine box-b while it was being fetched", self.gate(report, "taskDone")["reason"])

    def test_unreachable_worker_fails_closed_and_touches_nothing(self):
        self.worker.unreachable = {"all"}
        report = self.evaluate()
        self.assertFalse(report["harvestable"])
        self.assertIn("machine box-b is unreachable", self.gate(report, "noLiveSession")["reason"])
        self.assertFalse(any(o.startswith(("fetch", "push")) for o in self.order))

    def test_the_merge_test_runs_on_the_worker_at_the_pushed_pinned_base(self):
        self.project["checkCommand"] = "make test"
        self.project["checkTimeoutSeconds"] = 30
        self.worker.scratch = ok("centrale-merged\ncentrale-check-begin\nok\ncentrale-check-rc 0\n")
        report = self.evaluate()
        self.assertTrue(report["harvestable"], report)
        self.assertIn(f"push {BASE}:refs/heads/main", self.order)
        argv, timeout = self.worker.call("scratch")
        bare, base, base_sha, branch, tip, limit, grace, _sweep, check = argv[4:]
        self.assertEqual((base, base_sha, branch, tip), ("main", BASE, "task/task-7", TIP))
        self.assertEqual((limit, grace, check), ("30", str(remote_harvest.CHECK_KILL_GRACE_SECONDS), "make test"))
        self.assertIn('timeout -k "$grace" "$limit" sh -c "$check"', remote_harvest._SCRATCH_SCRIPT)
        self.assertGreater(timeout, 30 + remote_harvest.CHECK_KILL_GRACE_SECONDS)

    def test_a_timed_out_check_on_the_worker_fails_its_gate(self):
        self.project["checkCommand"] = "sleep 999"
        self.project["checkTimeoutSeconds"] = 5
        self.worker.scratch = ok("centrale-merged\ncentrale-check-begin\n\ncentrale-check-rc 124\n")
        report = self.evaluate()
        self.assertEqual(self.gate(report, "checkCommand")["reason"],
                         "checkCommand timed out after 5s on machine box-b (killed there)")

    def test_a_failing_check_reports_its_output(self):
        self.project["checkCommand"] = "make test"
        self.worker.scratch = ok("centrale-merged\ncentrale-check-begin\nFAILED test_x\ncentrale-check-rc 1\n")
        report = self.evaluate()
        self.assertEqual(self.gate(report, "checkCommand")["reason"],
                         "checkCommand failed on machine box-b: FAILED test_x")

    def test_the_worker_refuses_a_base_that_is_not_the_pinned_one(self):
        self.worker.scratch = ok("centrale-base-moved " + "d" * 40 + "\n", rc=20)
        report = self.evaluate()
        gate = self.gate(report, "mergeClean")
        self.assertFalse(gate["passed"])
        self.assertIn("the base moved", gate["reason"])

    def test_merge_conflict_on_the_worker(self):
        self.worker.scratch = ok(rc=23, stderr="CONFLICT (content): Merge conflict in a.txt")
        report = self.evaluate()
        self.assertEqual(self.gate(report, "mergeClean")["reason"],
                         "merge conflict: CONFLICT (content): Merge conflict in a.txt")

    def test_a_branch_gone_from_the_worker_is_already_merged(self):
        self.worker.tip = None
        report = self.evaluate()
        self.assertTrue(report["alreadyMerged"])

    def test_branches_are_enumerated_from_the_fetched_copies(self):
        calls = []

        def run_git(args, cwd=None):
            calls.append(args)
            if args[-1] == "refs/heads/task":  # the hub's own: task-9 is here too
                return subprocess.CompletedProcess(args, 0, "task/task-3\ntask/task-9\n", "")
            return subprocess.CompletedProcess(args, 0, f"{REF}\nrefs/remotes/box-b/task/task-9\n", "")

        with mock.patch.object(server, "run_git", run_git):
            self.assertEqual(harvest.task_branches(self.config, self.config["projects"][0]),
                             ["task/task-3", "task/task-9", "task/task-7"])
        self.assertEqual([c[-1] for c in calls], ["refs/heads/task", "refs/remotes/box-b/task"])


class HarvestTests(RemoteHarvestCase):
    def harvest(self):
        return harvest.harvest_branch(self.config, "app", "TASK-7")

    def test_merges_the_pinned_commit_on_the_hub_then_cleans_up_the_worker(self):
        report = self.harvest()
        self.assertTrue(report["merged"], report)
        merge = next(o for o in self.order if o.startswith("merge --no-ff"))
        self.assertTrue(merge.endswith(TIP))
        cleanup, _ = self.worker.call("cleanup")
        self.assertEqual(cleanup[4:9], ["~/centrale-repos/app/.git", "~/centrale-work/app-task-7",
                                        "task/task-7", TIP, "1"])
        self.assertIn(f"update-ref -d {REF}", self.order)
        self.assertIn("push refs/heads/main:refs/heads/main", self.order)
        self.assertTrue(self.gate(report, remote_harvest.STAGE_PINS_UNCHANGED)["passed"])

    def test_refused_when_the_branch_tip_moved_after_the_gates(self):
        self.worker.tips = [TIP, "c" * 40]
        report = self.harvest()
        self.assertFalse(report["merged"])
        self.assertIn("moved on machine box-b since the gates ran",
                      self.gate(report, remote_harvest.STAGE_PINS_UNCHANGED)["reason"])
        self.assertFalse(any(o.startswith("merge --no-ff") for o in self.order))
        self.assertNotIn("cleanup", self.worker.steps())

    def test_refused_when_the_base_moved_on_the_hub_after_the_gates(self):
        real = self.fake_run_git

        def moving(args, cwd=None):
            if args[-1] == "refs/heads/main" and any(o.startswith("push") for o in self.order):
                self.hub["refs/heads/main"] = "e" * 40
            return real(args, cwd)

        with mock.patch.object(server, "run_git", moving):
            report = self.harvest()
        self.assertFalse(report["merged"])
        self.assertIn("main moved on the hub since the gates ran",
                      self.gate(report, remote_harvest.STAGE_PINS_UNCHANGED)["reason"])
        self.assertFalse(any(o.startswith("merge --no-ff") for o in self.order))

    def test_an_unreachable_cleanup_after_the_merge_is_owed_with_the_tip(self):
        self.worker.unreachable = {"cleanup"}
        report = self.harvest()
        self.assertTrue(report["merged"])
        self.assertIn("recorded as owed", report["warnings"][0])
        owed = remote.owed_teardowns()
        self.assertEqual((owed[0]["kind"], owed[0]["expectedTip"]), ("cleanup", TIP))

    def test_board_side_actions_are_refused_for_a_remote_task(self):
        with self.assertRaises(harvest.HarvestError) as caught:
            harvest.harvest_branch(self.config, "app", "TASK-7", adopt_done=True)
        self.assertEqual(caught.exception.status, 409)


class ThrowawayTests(RemoteHarvestCase):
    def test_preview_names_the_workers_uncommitted_files_and_the_fetched_commits(self):
        self.worker.status = " M src/app.py\n?? notes.txt\n"
        status, body = remote_harvest.discard_preview(self.config, self.project, "TASK-7")
        self.assertEqual(status, 200)
        self.assertEqual((body["dirtyPaths"], body["dirtyFileCount"], body["commitCount"]),
                         (["src/app.py", "notes.txt"], 2, 2))
        self.assertEqual((body["branchTip"], body["machine"], body["worktreePath"]), (TIP, "box-b", WT))
        self.assertIn(f"fetch +refs/heads/task/task-7:{REF}", self.order)

    def test_discard_tags_the_fetched_tip_on_the_hub_before_removing_anything(self):
        self.worker.status = "?? notes.txt\n"
        status, body = remote_harvest.throw_away(self.config, self.project, "TASK-7", TIP, ["notes.txt"], "discard")
        self.assertEqual(status, 200, body)
        tag = next(o for o in self.order if o.startswith("tag "))
        self.assertTrue(tag.endswith(TIP))
        self.assertEqual(self.worker.steps()[-1], "throwaway")
        argv, _ = self.worker.call("throwaway")
        self.assertEqual(argv[4:9], ["~/centrale-repos/app/.git", "~/centrale-work/app-task-7",
                                     "task/task-7", TIP, "discard"])
        self.assertLess(self.order.index(tag), self.order.index(f"update-ref -d {REF}"))
        self.assertEqual((body["recoveryTag"], body["discardedPaths"], body["branchDeleted"]),
                         (tag.split()[1], ["notes.txt"], True))

    def test_a_confirm_that_no_longer_matches_the_worker_destroys_nothing(self):
        self.worker.status = "?? notes.txt\n?? more.txt\n"
        status, body = remote_harvest.throw_away(self.config, self.project, "TASK-7", TIP, ["notes.txt"], "discard")
        self.assertEqual(status, 409)
        self.assertIn("the worktree changed after that preview", body["error"])
        self.assertFalse(any(o.startswith("tag ") for o in self.order))
        self.assertNotIn("throwaway", self.worker.steps())

    def test_live_session_on_the_worker_refuses(self):
        self.worker.sessions = ["centrale-app-task-7"]
        status, body = remote_harvest.throw_away(self.config, self.project, "TASK-7", TIP, [], "discard")
        self.assertEqual(status, 409)
        self.assertNotIn("throwaway", self.worker.steps())

    def test_unreachable_worker_refuses_with_a_reason_and_tags_nothing(self):
        self.worker.unreachable = {"all"}
        status, body = remote_harvest.throw_away(self.config, self.project, "TASK-7", TIP, [], "discard")
        self.assertEqual(status, 503)
        self.assertIn("machine box-b is unreachable", body["error"])
        self.assertIn("nothing was discarded", body["error"])
        self.assertFalse(any(o.startswith(("tag ", "fetch")) for o in self.order))

    def test_abandon_removes_only_the_worktree(self):
        status, body = remote_harvest.throw_away(self.config, self.project, "TASK-7", TIP, [], "abandon")
        self.assertEqual(status, 200, body)
        self.assertFalse(any(o.startswith("tag ") for o in self.order))
        argv, _ = self.worker.call("throwaway")
        self.assertEqual(argv[8], "abandon")
        self.assertTrue(body["branchKept"])
        self.assertNotIn(f"update-ref -d {REF}", self.order)

    def test_a_tip_that_moved_on_the_worker_at_the_last_moment_is_refused(self):
        self.worker.throwaway = ok("centrale-moved " + "c" * 40 + "\n", rc=31)
        status, body = remote_harvest.throw_away(self.config, self.project, "TASK-7", TIP, [], "discard")
        self.assertEqual(status, 409)
        self.assertIn("Nothing was discarded", body["error"])


class BoardTests(RemoteHarvestCase):
    def placement(self, report):
        return remote_harvest.board_placement(self.config, self.config["projects"][0], report)

    def test_branch_checkouts_come_from_the_workers_round_trip(self):
        reads = remote_harvest.refresh_reads(self.config)
        self.assertEqual(len(reads["box-b"]), 2)
        porcelain = (f"worktree {WT}\nHEAD a\nbranch refs/heads/task/task-7\n\n"
                     f"worktree {HOME}/x\nHEAD a\nbranch refs/heads/task/task-8\n\n")
        report = [{"name": "box-b", "reach": "reachable", "error": None,
                   "results": [ok(HOME), ok(porcelain)]}]
        placement = self.placement(report)
        kinds = [placement.branch_checkout(t, "box-b")["kind"] for t in ("TASK-7", "TASK-8", "TASK-9")]
        self.assertEqual(kinds, ["centrale", "external", "none"])
        # TASK-7 and TASK-8 have worktrees there; TASK-9 only a fetched ref
        # would place it, and the hub holds none for it.
        self.assertEqual([placement.locate(t).machine for t in ("TASK-7", "TASK-8")], ["box-b", "box-b"])
        self.assertFalse(placement.locate("TASK-9").found)

    def test_unanswered_machine_reads_unknown_and_locates_by_the_fetched_ref(self):
        report = [{"name": "box-b", "reach": "unreachable", "error": "x",
                   "results": [MachineResult(UNREACHED, None, "", "x"), MachineResult(UNREACHED, None, "", "x")]}]
        with mock.patch.object(machines, "fetched_task_branches", lambda project, name: {"task/task-7"}):
            placement = self.placement(report)
        self.assertEqual(placement.branch_checkout("TASK-7", "box-b")["kind"], "unknown")
        location = placement.locate("TASK-7")
        self.assertEqual(location.as_json(), {"machine": "box-b", "conflict": None, "reach": "unreachable"})


if __name__ == "__main__":
    unittest.main()
