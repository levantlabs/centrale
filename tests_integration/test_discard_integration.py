"""Integration coverage for task-119's two ways out of a bad attempt,
against a real, sandboxed server.py process: POST /api/discard-attempt
(worktree AND branch go) and POST /api/abandon-worktree (only the
worktree goes).

Two claims here cannot be made hermetically, and both are the reason
this file exists:

  * The recovery tag is load-bearing. `git worktree remove --force`
    followed by `git branch -D` leaves NO reflog anywhere referencing the
    branch tip -- the worktree's reflog goes with the worktree and the
    branch's with the branch -- so a `git gc --prune=now` afterwards
    destroys the commits, and the "recover with `git branch ... <sha>`"
    line the response hands the user would be a promise git does not
    keep. The tag is what keeps it. That is only demonstrable against a
    real git object store, so it is demonstrated here: gc first, then
    recover.
  * "The next spawn branches fresh from the base" is a property of
    spawn._ensure_worktree meeting a repository, not of a mock. It is
    checked by running that function for real, before and after.

Real git and the real `backlog` CLI; the tmux PATH shim keeps the
live-session check off the user's tmux server; ephemeral port; every
path under a private tmp dir. No agent is ever spawned.

Run explicitly: python3 -m unittest tests_integration.test_discard_integration
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import unittest
import urllib.error
import urllib.request

from tests_integration import base

sys.path.insert(0, base.CENTRALE_ROOT)
import spawn  # noqa: E402


def tearDownModule():
    base.assert_test_tmux_footprint_gone()


@base.require_tools("git", "backlog")
class DiscardAttemptIntegrationTests(base.IntegrationCase):
    PROJECT = "discardproj"

    def setUp(self):
        super().setUp()
        self.repo_path = base.init_backlog_repo(self.tmp_dir, name=self.PROJECT)
        self.app_dir = base.deploy_app(os.path.join(self.tmp_dir, "app"))
        self.server_port = base.free_port()
        self.worktree_root = os.path.join(self.tmp_dir, "worktrees")
        self.config = {
            "port": self.server_port,
            "worktreeRoot": self.worktree_root,
            "projects": [{"name": self.PROJECT, "path": self.repo_path}],
        }
        base.write_projects_json(self.app_dir, self.config)

    # -- helpers ------------------------------------------------------------

    def _start_server(self):
        proc = subprocess.Popen(
            [sys.executable, "server.py"],
            cwd=self.app_dir,
            env=self.env(),  # tmux-sandboxed PATH + isolated XDG_CACHE_HOME
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        self.track_proc(proc)
        self.wait_for_http(f"http://127.0.0.1:{self.server_port}/api/board", timeout=15.0)
        return proc

    def _request(self, method, path, body=None):
        """(status, decoded JSON) -- a non-2xx status is returned, never
        raised, so tests can assert on the 409 body."""
        data = None if body is None else json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.server_port}{path}",
            data=data,
            headers={"Content-Type": "application/json"} if data is not None else {},
            method=method,
        )
        try:
            with urllib.request.urlopen(req, timeout=15.0) as resp:
                return resp.status, json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read() or b"{}")

    def _reviewed(self, task_id):
        """The body a confirming click carries (task-121): identity plus
        the exact state GET /api/discard-preview just reported. The two
        destructive routes require it -- a POST that names no state has
        reviewed none, which is the gap the refusals below close."""
        status, preview = self._request(
            "GET", f"/api/discard-preview?project={self.PROJECT}&task={task_id}")
        self.assertEqual(status, 200, preview)
        return {"project": self.PROJECT, "taskId": task_id,
                "expectedBranchTip": preview["branchTip"],
                "expectedDirtyPaths": preview["dirtyPaths"]}

    def _git(self, *args, cwd=None, check=True):
        return base.run(["git", *args], cwd=cwd or self.repo_path, check=check)

    def _branch_exists(self, branch):
        return self._git("rev-parse", "--verify", "--quiet", f"refs/heads/{branch}",
                         check=False).returncode == 0

    def _worktree_paths(self):
        out = self._git("worktree", "list", "--porcelain").stdout
        return {os.path.realpath(line[len("worktree "):])
                for line in out.splitlines() if line.startswith("worktree ")}

    def _object_exists(self, sha):
        return self._git("cat-file", "-e", f"{sha}^{{commit}}", check=False).returncode == 0

    def _bad_attempt(self, checkout_dir=None):
        """A task whose task/<id> branch carries two commits nobody wants
        and whose worktree has three uncommitted paths. Returns
        (task_id, branch, worktree_dir, tip_sha).

        `checkout_dir` defaults to Centrale's own worktree path; passing
        a foreign one produces the external-checkout state instead."""
        task_id = base.create_task(self.repo_path, "An attempt that went wrong",
                                   acceptance_criteria=["it works"])
        self._git("add", "-A")
        self._git("commit", "-q", "-m", f"add {task_id}")
        branch = spawn.branch_name(task_id)
        wt_dir = checkout_dir or spawn.worktree_dir(self.config, self.PROJECT, task_id)
        os.makedirs(os.path.dirname(wt_dir), exist_ok=True)
        self._git("worktree", "add", "-q", "-b", branch, wt_dir, "main")

        for n in (1, 2):
            with open(os.path.join(wt_dir, f"wrong-{n}.py"), "w", encoding="utf-8") as f:
                f.write(f"# attempt {n}, and it is wrong\n")
            self._git("add", "-A", cwd=wt_dir)
            self._git("commit", "-q", "-m", f"{task_id}: wrong turn {n}", cwd=wt_dir)

        # Two untracked and one modified: uncommitted work that a forced
        # removal really would destroy (the TASK-9 lesson), including a
        # realistically spaced Backlog.md filename.
        with open(os.path.join(wt_dir, "scratch notes.md"), "w", encoding="utf-8") as f:
            f.write("half-finished thinking\n")
        with open(os.path.join(wt_dir, "another.tmp"), "w", encoding="utf-8") as f:
            f.write("tmp\n")
        with open(os.path.join(wt_dir, "wrong-1.py"), "a", encoding="utf-8") as f:
            f.write("# and edited since\n")

        tip = self._git("rev-parse", branch).stdout.strip()
        return task_id, branch, wt_dir, tip

    # -- tests --------------------------------------------------------------

    def test_discard_removes_both_and_the_tag_survives_a_prune_so_recovery_really_works(self):
        task_id, branch, wt_dir, tip = self._bad_attempt()
        self._start_server()

        status, body = self._request("POST", "/api/discard-attempt",
                                     self._reviewed(task_id))

        self.assertEqual(status, 200, body)
        self.assertEqual(body["branch"], branch)
        self.assertEqual(body["branchTip"], tip)
        self.assertEqual(body["commitCount"], 2)
        self.assertEqual(body["baseBranch"], "main")
        self.assertTrue(body["worktreeRemoved"])
        self.assertTrue(body["branchDeleted"])
        self.assertEqual(sorted(body["discardedPaths"]),
                         ["another.tmp", "scratch notes.md", "wrong-1.py"])
        self.assertEqual(body["recoveryCommand"], f"git branch {branch} {tip}")
        self.assertRegex(body["recoveryTag"],
                         r"^abandoned/" + re.escape(task_id.lower()) + r"-\d{8}-\d{6}$")

        # Both really gone, and the main checkout untouched.
        self.assertFalse(self._branch_exists(branch))
        self.assertFalse(os.path.isdir(wt_dir))
        self.assertNotIn(os.path.realpath(wt_dir), self._worktree_paths())
        self.assertEqual(self._git("status", "--porcelain").stdout.strip(), "")

        # The measured claim: without the tag these commits would be
        # unreachable and prunable the instant the branch went. With it,
        # the most aggressive gc git offers leaves them alone...
        self.assertEqual(
            self._git("rev-parse", body["recoveryTag"] + "^{commit}").stdout.strip(), tip)
        self._git("reflog", "expire", "--expire-unreachable=now", "--all")
        self._git("gc", "--prune=now", "-q")
        self.assertTrue(self._object_exists(tip),
                        "the recovery tag must keep the discarded commits alive through a gc")

        # ...so the command handed to the user actually puts the branch
        # back, with its work intact.
        self._git(*body["recoveryCommand"].split()[1:])
        self.assertTrue(self._branch_exists(branch))
        self.assertEqual(self._git("rev-parse", branch).stdout.strip(), tip)
        self.assertIn("wrong turn 2", self._git("log", "-1", "--format=%s", branch).stdout)

    def test_the_next_spawn_branches_fresh_from_the_base_after_a_discard(self):
        # AC #3's real subject, and the reason the branch has to go and
        # not just the worktree: spawn._ensure_worktree REUSES an
        # existing task/<id> branch, so a parked one would hand every
        # re-spawn the same bad commits back.
        task_id, branch, wt_dir, tip = self._bad_attempt()
        base_tip = self._git("rev-parse", "main").stdout.strip()
        self._start_server()

        status, body = self._request("POST", "/api/discard-attempt",
                                     self._reviewed(task_id))
        self.assertEqual(status, 200, body)

        project = self.config["projects"][0]
        fresh_wt = spawn._ensure_worktree(self.config, project, task_id)
        self.addCleanup(lambda: self._git("worktree", "remove", "--force", fresh_wt, check=False))

        self.assertEqual(os.path.realpath(fresh_wt), os.path.realpath(wt_dir))
        # A brand new branch at the base, carrying none of the attempt.
        self.assertEqual(self._git("rev-parse", branch).stdout.strip(), base_tip)
        self.assertEqual(
            self._git("rev-list", "--count", f"main..{branch}").stdout.strip(), "0")
        self.assertFalse(os.path.exists(os.path.join(fresh_wt, "wrong-1.py")))

        # The board agrees: the task has a branch again, and it is a
        # Centrale checkout rather than the discarded one's ghost.
        status, board = self._request("GET", "/api/board")
        self.assertEqual(status, 200)
        project_data = next(p for p in board["projects"] if p["name"] == self.PROJECT)
        task = next(t for t in project_data["tasks"] if t["id"] == task_id)
        self.assertTrue(task["hasSpawnBranch"])
        self.assertEqual(task["branchCheckout"]["kind"], "centrale")

    def test_the_board_offers_the_ordinary_spawn_again_between_the_discard_and_the_respawn(self):
        # The same AC from the page's side: with no branch at all, the
        # board reports exactly the state the frontend renders a plain
        # Spawn button for.
        task_id, branch, wt_dir, tip = self._bad_attempt()
        self._start_server()

        status, board = self._request("GET", "/api/board")
        before = next(t for t in next(p for p in board["projects"]
                                      if p["name"] == self.PROJECT)["tasks"]
                      if t["id"] == task_id)
        self.assertTrue(before["hasSpawnBranch"])
        self.assertTrue(before["worktreeDirty"])

        status, body = self._request("POST", "/api/discard-attempt",
                                     self._reviewed(task_id))
        self.assertEqual(status, 200, body)

        status, board = self._request("GET", "/api/board?force=1")
        after = next(t for t in next(p for p in board["projects"]
                                     if p["name"] == self.PROJECT)["tasks"]
                     if t["id"] == task_id)
        self.assertFalse(after["hasSpawnBranch"])
        self.assertFalse(after["worktreeDirty"])
        self.assertIsNone(after["branchCheckout"])
        self.assertFalse(after["alreadyMerged"])
        # AC #2: the status is exactly what it was. Discarding an attempt
        # is not a statement about where the task stands.
        self.assertEqual(after["status"], before["status"])

    def test_the_preview_names_what_the_discard_then_actually_destroys(self):
        # AC #4's chain, end to end: the numbers the confirming click
        # shows are the ones the destructive call reports back.
        task_id, branch, wt_dir, tip = self._bad_attempt()
        self._start_server()

        status, preview = self._request(
            "GET", f"/api/discard-preview?project={self.PROJECT}&task={task_id}")
        self.assertEqual(status, 200, preview)
        self.assertEqual(preview["commitCount"], 2)
        self.assertEqual(preview["dirtyFileCount"], 3)
        self.assertEqual(preview["branchTip"], tip)
        self.assertIsNone(preview["liveSession"])
        self.assertIsNone(preview["externalCheckout"])

        # Purely descriptive: nothing moved.
        self.assertTrue(self._branch_exists(branch))
        self.assertTrue(os.path.isdir(wt_dir))

        # Bound to THAT preview (task-121), not to a second measurement
        # taken behind the user's back.
        status, body = self._request("POST", "/api/discard-attempt", {
            "project": self.PROJECT, "taskId": task_id,
            "expectedBranchTip": preview["branchTip"],
            "expectedDirtyPaths": preview["dirtyPaths"],
        })
        self.assertEqual(status, 200, body)
        self.assertEqual(body["commitCount"], preview["commitCount"])
        self.assertEqual(len(body["discardedPaths"]), preview["dirtyFileCount"])
        self.assertEqual(sorted(body["discardedPaths"]), sorted(preview["dirtyPaths"]))

    def test_abandon_leaves_a_parked_branch_the_board_still_sees(self):
        task_id, branch, wt_dir, tip = self._bad_attempt()
        self._start_server()

        status, body = self._request("POST", "/api/abandon-worktree",
                                     self._reviewed(task_id))

        self.assertEqual(status, 200, body)
        self.assertTrue(body["worktreeRemoved"])
        self.assertTrue(body["branchKept"])
        self.assertEqual(body["commitCount"], 2)
        self.assertEqual(sorted(body["discardedPaths"]),
                         ["another.tmp", "scratch notes.md", "wrong-1.py"])

        # The worktree is gone; the commits are not, and no recovery tag
        # was needed because nothing was destroyed that git would forget.
        self.assertFalse(os.path.isdir(wt_dir))
        self.assertNotIn(os.path.realpath(wt_dir), self._worktree_paths())
        self.assertTrue(self._branch_exists(branch))
        self.assertEqual(self._git("rev-parse", branch).stdout.strip(), tip)
        self.assertEqual(self._git("tag", "--list", "abandoned/*").stdout.strip(), "")

        # task-70's parked kind: a branch checked out nowhere, which the
        # worktree-less gated merge can still merge later.
        status, board = self._request("GET", "/api/board?force=1")
        task = next(t for t in next(p for p in board["projects"]
                                    if p["name"] == self.PROJECT)["tasks"]
                    if t["id"] == task_id)
        self.assertTrue(task["hasSpawnBranch"])
        self.assertEqual(task["branchCheckout"], {"kind": "none", "path": None})

        # And a discard afterwards still finishes the job.
        status, body = self._request("POST", "/api/discard-attempt",
                                     self._reviewed(task_id))
        self.assertEqual(status, 200, body)
        self.assertFalse(body["worktreeRemoved"])
        self.assertTrue(body["branchDeleted"])
        self.assertFalse(self._branch_exists(branch))

    def test_a_commit_landing_between_the_preview_and_the_click_destroys_nothing(self):
        # task-121's reproduction, against real git: the preview reported
        # 2 commits and 3 uncommitted files, the agent committed once
        # more, and the already-armed confirm went ahead and destroyed a
        # commit it had never named. Now it refuses, and the numbers the
        # user reads next are the ones that are true.
        task_id, branch, wt_dir, tip = self._bad_attempt()
        self._start_server()

        armed = self._reviewed(task_id)
        self.assertEqual(armed["expectedBranchTip"], tip)

        # The agent commits again while the confirm sits armed.
        self._git("add", "-A", cwd=wt_dir)
        self._git("commit", "-q", "-m", f"{task_id}: one more, after the preview", cwd=wt_dir)
        moved_tip = self._git("rev-parse", branch).stdout.strip()
        self.assertNotEqual(moved_tip, tip)

        status, body = self._request("POST", "/api/discard-attempt", armed)
        self.assertEqual(status, 409, body)
        self.assertIn("has moved", body["error"])
        self.assertIn(moved_tip[:10], body["error"])
        self.assertIn("Nothing was discarded", body["error"])

        # And nothing was: no tag, no removal, no delete.
        self.assertEqual(self._git("tag", "--list", "abandoned/*").stdout.strip(), "")
        self.assertTrue(self._branch_exists(branch))
        self.assertEqual(self._git("rev-parse", branch).stdout.strip(), moved_tip)
        self.assertTrue(os.path.isdir(wt_dir))

        # A fresh preview reports the state as it now is, and a confirm
        # armed off THAT one goes through.
        rearmed = self._reviewed(task_id)
        self.assertEqual(rearmed["expectedBranchTip"], moved_tip)
        status, body = self._request("POST", "/api/discard-attempt", rearmed)
        self.assertEqual(status, 200, body)
        self.assertEqual(body["commitCount"], 3)
        self.assertEqual(body["branchTip"], moved_tip)
        self.assertEqual(
            self._git("rev-parse", body["recoveryTag"] + "^{commit}").stdout.strip(), moved_tip)

    def test_a_file_written_between_the_preview_and_the_click_destroys_nothing(self):
        # The other half of the confirm's sentence. An untracked file
        # written after the preview is one the user never agreed to lose,
        # and a forced worktree removal really would take it.
        task_id, branch, wt_dir, tip = self._bad_attempt()
        self._start_server()

        armed = self._reviewed(task_id)
        self.assertEqual(len(armed["expectedDirtyPaths"]), 3)

        late = os.path.join(wt_dir, "written after the preview.md")
        with open(late, "w", encoding="utf-8") as f:
            f.write("work nobody has agreed to lose\n")

        for path in ("/api/discard-attempt", "/api/abandon-worktree"):
            with self.subTest(path=path):
                status, body = self._request("POST", path, armed)
                self.assertEqual(status, 409, body)
                self.assertIn("4 uncommitted files now, not the 3", body["error"])
                self.assertTrue(os.path.isfile(late))
                self.assertTrue(os.path.isdir(wt_dir))
                self.assertTrue(self._branch_exists(branch))
                self.assertEqual(self._git("tag", "--list", "abandoned/*").stdout.strip(), "")

        status, body = self._request("POST", "/api/abandon-worktree", self._reviewed(task_id))
        self.assertEqual(status, 200, body)
        self.assertEqual(len(body["discardedPaths"]), 4)

    def test_two_discards_at_once_leave_one_tag_at_the_tip_that_was_deleted(self):
        # Two armed confirms for the same task -- two browser tabs, or a
        # double click that outran its own re-render. Centrale serves
        # them concurrently, so without the per-task lifecycle lock they
        # interleave: two tags, or a branch deleted while the other
        # request is still surveying it. Serialized, exactly one does the
        # work and the other finds nothing left to do.
        import threading

        task_id, branch, wt_dir, tip = self._bad_attempt()
        self._start_server()
        armed = self._reviewed(task_id)

        results = {}
        barrier = threading.Barrier(2)

        def attempt(label):
            def run():
                barrier.wait(10)
                results[label] = self._request("POST", "/api/discard-attempt", dict(armed))
            return threading.Thread(target=run, name=label, daemon=True)

        threads = [attempt("a"), attempt("b")]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
            self.assertFalse(t.is_alive(), "a concurrent discard never returned")

        statuses = sorted(status for status, _ in results.values())
        self.assertEqual(statuses, [200, 404], results)
        winner = next(body for status, body in results.values() if status == 200)
        loser = next(body for status, body in results.values() if status != 200)
        self.assertIn("no worktree or branch found", loser["error"])

        # One discard happened, once: one tag, at exactly the tip the
        # winner reported deleting, and nothing half-removed.
        tags = self._git("tag", "--list", "abandoned/*").stdout.split()
        self.assertEqual(tags, [winner["recoveryTag"]])
        self.assertEqual(self._git("rev-parse", tags[0] + "^{commit}").stdout.strip(), tip)
        self.assertEqual(winner["branchTip"], tip)
        self.assertFalse(self._branch_exists(branch))
        self.assertFalse(os.path.isdir(wt_dir))
        self.assertNotIn(os.path.realpath(wt_dir), self._worktree_paths())
        self.assertEqual(self._git("status", "--porcelain").stdout.strip(), "")

    def test_a_confirm_that_names_no_state_is_refused_before_anything_moves(self):
        task_id, branch, wt_dir, tip = self._bad_attempt()
        self._start_server()

        for path in ("/api/discard-attempt", "/api/abandon-worktree"):
            with self.subTest(path=path):
                status, body = self._request(
                    "POST", path, {"project": self.PROJECT, "taskId": task_id})
                self.assertEqual(status, 400, body)
                self.assertIn("expectedBranchTip", body["error"])
                self.assertIn("/api/discard-preview", body["error"])

        self.assertTrue(self._branch_exists(branch))
        self.assertTrue(os.path.isdir(wt_dir))
        self.assertEqual(self._git("tag", "--list", "abandoned/*").stdout.strip(), "")

    def test_both_routes_refuse_a_branch_checked_out_outside_centrale(self):
        foreign = os.path.join(self.tmp_dir, ".worktrees", "someone-elses-checkout")
        task_id, branch, wt_dir, tip = self._bad_attempt(checkout_dir=foreign)
        self.addCleanup(lambda: self._git("worktree", "remove", "--force", foreign, check=False))
        self._start_server()

        for path in ("/api/discard-attempt", "/api/abandon-worktree"):
            with self.subTest(path=path):
                status, body = self._request("POST", path, self._reviewed(task_id))
                self.assertEqual(status, 409, body)
                self.assertEqual(body["error"],
                                 spawn.external_checkout_reason(branch, foreign))
                self.assertNotIn("used by worktree at", body["error"])

        # Nothing was damaged by either refusal.
        self.assertTrue(self._branch_exists(branch))
        self.assertIn(os.path.realpath(foreign), self._worktree_paths())
        self.assertTrue(os.path.isfile(os.path.join(foreign, "wrong-1.py")))
        self.assertEqual(self._git("tag", "--list", "abandoned/*").stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
