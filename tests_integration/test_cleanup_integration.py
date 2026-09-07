"""Integration coverage for POST /api/cleanup-branch against a real,
sandboxed server.py process (task-80): the TASK-74 reproduction -- a
task/<id> branch merged out-of-band (`--no-ff`), its task Done on main,
the branch still checked out in a worktree Centrale does not manage, and
no Centrale worktree dir at all -- must now get the honest 409 carrying
spawn.external_checkout_reason (the exact sentence /api/spawn and
/api/resume use) and leave both the branch and the foreign worktree
intact, instead of the raw `git branch -d` refusal as a 500. The parked
(`none`) and Centrale-worktree (`centrale`) kinds keep cleaning up.

Real git and the real `backlog` CLI; the tmux PATH shim keeps the
live-session check off the user's tmux server; ephemeral port; every
path under a private tmp dir. No agent is ever spawned.

Run explicitly: python3 -m unittest tests_integration.test_cleanup_integration
"""

from __future__ import annotations

import json
import os
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
class CleanupBranchIntegrationTests(base.IntegrationCase):
    PROJECT = "cleanupproj"

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

    def _git(self, *args, cwd=None, check=True):
        return base.run(["git", *args], cwd=cwd or self.repo_path, check=check)

    def _merge_out_of_band(self, checkout_dir):
        """The TASK-74 scenario: a task on main, its task/<id> branch
        created straight into `checkout_dir` (a path Centrale never
        manages unless the caller passes its own worktree_dir), work +
        `backlog task edit -s Done` committed there, and the branch
        merged into main with --no-ff by hand. Returns (task_id, branch)."""
        task_id = base.create_task(self.repo_path, "Merged out of band", acceptance_criteria=["done"])
        self._git("add", "-A")
        self._git("commit", "-q", "-m", f"add {task_id}")
        branch = spawn.branch_name(task_id)
        self._git("worktree", "add", "-q", "-b", branch, checkout_dir, "main")
        with open(os.path.join(checkout_dir, "work.txt"), "w", encoding="utf-8") as f:
            f.write("work done outside Centrale\n")
        base.run(["backlog", "task", "edit", task_id, "-s", "Done", "--check-ac", "1"], cwd=checkout_dir)
        self._git("add", "-A", cwd=checkout_dir)
        self._git("commit", "-q", "-m", f"{task_id}: work", cwd=checkout_dir)
        self._git("merge", "--no-ff", "-q", "-m", f"merge {branch}", branch)
        return task_id, branch

    def _branch_exists(self, branch):
        return self._git("rev-parse", "--verify", "--quiet", f"refs/heads/{branch}", check=False).returncode == 0

    def _worktree_paths(self):
        out = self._git("worktree", "list", "--porcelain").stdout
        return {os.path.realpath(line[len("worktree "):]) for line in out.splitlines() if line.startswith("worktree ")}

    def _board_task(self, task_id):
        status, board = self._request("GET", "/api/board")
        self.assertEqual(status, 200)
        project = next(p for p in board["projects"] if p["name"] == self.PROJECT)
        return next(t for t in project["tasks"] if t["id"] == task_id)

    # -- tests --------------------------------------------------------------

    def test_external_checkout_gets_409_with_the_spawn_reason_and_nothing_is_touched(self):
        foreign = os.path.join(self.tmp_dir, ".worktrees", "someone-elses-checkout")
        task_id, branch = self._merge_out_of_band(foreign)
        centrale_wt = spawn.worktree_dir(self.config, self.PROJECT, task_id)
        self.assertFalse(os.path.isdir(centrale_wt), "the Centrale worktree dir must never have existed")
        self._start_server()

        # The board offers cleanup (alreadyMerged) AND flags the branch as
        # external at the same time -- exactly TASK-74's observation.
        task = self._board_task(task_id)
        self.assertTrue(task["alreadyMerged"])
        self.assertEqual(task["branchCheckout"]["kind"], "external")
        reported_path = task["branchCheckout"]["path"]
        self.assertEqual(os.path.realpath(reported_path), os.path.realpath(foreign))

        status, body = self._request("POST", "/api/cleanup-branch", {"project": self.PROJECT, "taskId": task_id})

        self.assertEqual(status, 409, body)
        self.assertEqual(body["error"], spawn.external_checkout_reason(branch, reported_path))
        self.assertNotIn("used by worktree at", body["error"])
        self.assertNotIn("checked out at", body["error"].replace("checked out outside", ""))

        # Nothing was damaged: branch still there, foreign worktree still
        # registered and its files untouched, Centrale dir still absent.
        self.assertTrue(self._branch_exists(branch))
        self.assertIn(os.path.realpath(foreign), self._worktree_paths())
        self.assertTrue(os.path.isfile(os.path.join(foreign, "work.txt")))
        self.assertFalse(os.path.isdir(centrale_wt))
        # Merge state unchanged too: main's tip is still the --no-ff merge.
        self.assertIn(f"merge {branch}", self._git("log", "-1", "--format=%s").stdout)

    def test_removing_the_foreign_worktree_makes_the_same_branch_cleanable_as_parked(self):
        # The self-healing half of the story: once the foreign checkout is
        # gone the branch is a parked (`none`) one, and the very same
        # request now succeeds with today's branch-only cleanup.
        foreign = os.path.join(self.tmp_dir, ".worktrees", "someone-elses-checkout")
        task_id, branch = self._merge_out_of_band(foreign)
        self._git("worktree", "remove", "--force", foreign)
        self._start_server()

        task = self._board_task(task_id)
        self.assertTrue(task["alreadyMerged"])
        self.assertEqual(task["branchCheckout"], {"kind": "none", "path": None})

        status, body = self._request("POST", "/api/cleanup-branch", {"project": self.PROJECT, "taskId": task_id})

        self.assertEqual(status, 200, body)
        self.assertEqual(body["branch"], branch)
        self.assertFalse(body["worktreeRemoved"])
        self.assertEqual(body["discardedPaths"], [])
        self.assertFalse(self._branch_exists(branch))

    def test_centrale_checkout_still_removes_the_worktree_and_deletes_the_branch(self):
        # kind == "centrale": the out-of-band merge happened from Centrale's
        # own worktree path -> today's full cleanup, with an untracked
        # leftover reported in discardedPaths.
        task_id = base.create_task(self.repo_path, "Merged from the Centrale worktree", acceptance_criteria=["done"])
        # _merge_out_of_band creates its own task; build the path first so
        # the branch lands exactly where checkout_state calls "centrale".
        self._git("add", "-A")
        self._git("commit", "-q", "-m", f"add {task_id}")
        centrale_wt = spawn.worktree_dir(self.config, self.PROJECT, task_id)
        os.makedirs(os.path.dirname(centrale_wt), exist_ok=True)
        branch = spawn.branch_name(task_id)
        self._git("worktree", "add", "-q", "-b", branch, centrale_wt, "main")
        base.run(["backlog", "task", "edit", task_id, "-s", "Done", "--check-ac", "1"], cwd=centrale_wt)
        self._git("add", "-A", cwd=centrale_wt)
        self._git("commit", "-q", "-m", f"{task_id}: done", cwd=centrale_wt)
        self._git("merge", "--no-ff", "-q", "-m", f"merge {branch}", branch)
        with open(os.path.join(centrale_wt, "scratch.txt"), "w", encoding="utf-8") as f:
            f.write("untracked leftover\n")
        self._start_server()

        task = self._board_task(task_id)
        self.assertTrue(task["alreadyMerged"])
        self.assertEqual(task["branchCheckout"]["kind"], "centrale")

        status, body = self._request("POST", "/api/cleanup-branch", {"project": self.PROJECT, "taskId": task_id})

        self.assertEqual(status, 200, body)
        self.assertTrue(body["worktreeRemoved"])
        self.assertEqual(body["discardedPaths"], ["scratch.txt"])
        self.assertFalse(os.path.isdir(centrale_wt))
        self.assertFalse(self._branch_exists(branch))
        self.assertNotIn(os.path.realpath(centrale_wt), self._worktree_paths())


if __name__ == "__main__":
    unittest.main()
