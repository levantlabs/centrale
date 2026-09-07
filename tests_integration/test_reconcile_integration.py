"""Integration coverage for task-66 (resume-to-reconcile) against real
git, a real `backlog` CLI, and a real tmux session running a harmless
probe -- never a real agent:

- a genuine *semantic* collision: a finished task branch that is green
  on its own, a base branch that has since landed a check the branch
  fails, and git seeing no textual conflict at all. evaluate_branch()
  must fail gate 5 on the scratch-merged tree AND report how far the
  branch is behind, with the collision hint;
- an up-to-date branch failing the same gate must NOT carry the hint;
- spawn.resume(..., reconcile=True) must start the resolved agent's
  RESUME command (task-133: its configured resumeCmd here) in the
  existing worktree with the reconcile prompt appended as the trailing
  argument, while Centrale itself merges nothing: the branch tip and its
  behind-count are untouched.

Run explicitly: python3 -m unittest tests_integration.test_reconcile_integration
"""

from __future__ import annotations

import os
import sys
import unittest

from tests_integration import base

sys.path.insert(0, base.CENTRALE_ROOT)
import harvest  # noqa: E402
import server  # noqa: E402
import spawn  # noqa: E402


def tearDownModule():
    base.assert_test_tmux_footprint_gone()


@base.require_tools("git", "tmux", "backlog")
class ReconcileIntegrationTests(base.IntegrationCase):
    def setUp(self):
        super().setUp()
        self.repo_path = base.init_backlog_repo(self.tmp_dir, name="reconproj")
        self.worktree_root = os.path.join(self.tmp_dir, "worktrees")
        # The check is a script that lives on MAIN only (landed after the
        # branch was cut). `sh check.sh` is what gate 5 runs in the
        # scratch-merged tree, where both the script and the branch's
        # work.txt exist together.
        self.project = {"name": "reconproj", "path": self.repo_path, "checkCommand": "sh check.sh"}
        self.config = {
            "worktreeRoot": self.worktree_root,
            "projects": [self.project],
            "agents": server.normalize_agents_map({
                "claude": ["claude"],
                # The prompt becomes `sh -c`'s "$1" -- inert, but visible
                # in the pane's cmdline for the assertions below. Both
                # forms are wrapped: task-133 appends the reconcile prompt
                # to the resumeCmd too, and a bare `sleep 277` would die
                # on it with 'invalid time interval' (see probe_spawn_cmd).
                "recon-agent": {"cmd": ["sh", "-c", "sleep 233", "itest-reconcile"],
                                "resumeCmd": ["sh", "-c", "sleep 277", "itest-reconcile-resume"]},
            }),
            "defaultAgent": "claude",
            "capabilities": {"tmux": True},
        }

    def _commit_all(self, cwd, message):
        base.run(["git", "add", "-A"], cwd=cwd)
        base.run(["git", "commit", "-q", "-m", message], cwd=cwd)

    def _make_finished_branch(self, task_id):
        """A real worktree + branch cut from main with the task claimed,
        then finished there: work.txt added, task Done with its AC
        checked, everything committed -- green in isolation."""
        self._commit_all(self.repo_path, f"add {task_id}")
        branch = spawn.branch_name(task_id)
        wt_dir = spawn.worktree_dir(self.config, "reconproj", task_id)
        os.makedirs(self.worktree_root, exist_ok=True)
        base.run(["git", "worktree", "add", "-b", branch, wt_dir, "main"], cwd=self.repo_path)
        with open(os.path.join(wt_dir, "work.txt"), "w", encoding="utf-8") as f:
            f.write("done\n")
        base.run(["backlog", "task", "edit", task_id, "-s", "Done", "--check-ac", "1", "--plain"], cwd=wt_dir)
        self._commit_all(wt_dir, f"finish {task_id}")
        return wt_dir, branch

    def _land_colliding_check_on_main(self):
        """Two newer commits on main the branch doesn't have; the first
        lands a check asserting work.txt must NOT exist -- exactly the
        kind of thing a parallel branch legitimately changes. No file is
        touched on both sides, so git sees no textual conflict."""
        with open(os.path.join(self.repo_path, "check.sh"), "w", encoding="utf-8") as f:
            f.write("#!/bin/sh\ntest ! -e work.txt\n")
        self._commit_all(self.repo_path, "land a check that collides semantically")
        with open(os.path.join(self.repo_path, "newer.txt"), "w", encoding="utf-8") as f:
            f.write("unrelated newer work\n")
        self._commit_all(self.repo_path, "unrelated newer work on main")

    def _behind_count(self, branch):
        proc = base.run(["git", "rev-list", "--count", f"{branch}..main"], cwd=self.repo_path)
        return int(proc.stdout.strip())

    def _session_alive(self, name):
        proc = base.tmux("list-sessions", "-F", "#{session_name}", check=False)
        if proc.returncode != 0:
            return False
        return name in {line.strip() for line in proc.stdout.splitlines() if line.strip()}

    def test_semantic_collision_reports_behind_count_and_hint(self):
        task_id = base.create_task(self.repo_path, "Collides later", acceptance_criteria=["do the thing"],
                                   assignee="recon-agent")
        wt_dir, branch = self._make_finished_branch(task_id)
        self._land_colliding_check_on_main()
        self.assertEqual(self._behind_count(branch), 2)

        report = harvest.evaluate_branch(self.config, self.project, task_id)

        self.assertFalse(report["harvestable"], report)
        gate_status = {g["name"]: g["passed"] for g in report["gates"]}
        # Green on its own and textually clean -- only the combination fails.
        self.assertIs(gate_status["mergeClean"], True, report["gates"])
        self.assertIs(gate_status["checkCommand"], False, report["gates"])
        self.assertEqual(report["behindBase"], {"baseBranch": "main", "count": 2})
        self.assertEqual(
            report["reconcileHint"],
            "this branch predates 2 newer commits on main; the failure may be a "
            "collision with newer work, not a defect in the branch",
        )
        # Read-only: nothing moved.
        self.assertEqual(self._behind_count(branch), 2)
        self.assertTrue(os.path.isdir(wt_dir))

    def test_up_to_date_failing_branch_has_no_hint(self):
        task_id = base.create_task(self.repo_path, "Just broken", acceptance_criteria=["do the thing"])
        _, branch = self._make_finished_branch(task_id)
        # The branch itself carries a failing check; main has nothing newer.
        self.project["checkCommand"] = "false"
        self.assertEqual(self._behind_count(branch), 0)

        report = harvest.evaluate_branch(self.config, self.project, task_id)

        self.assertFalse(report["harvestable"])
        self.assertIs({g["name"]: g["passed"] for g in report["gates"]}["checkCommand"], False)
        self.assertNotIn("behindBase", report)
        self.assertNotIn("reconcileHint", report)

    def test_reconcile_resume_launches_reconcile_prompt_and_merges_nothing_itself(self):
        task_id = base.create_task(self.repo_path, "Reconcile me", acceptance_criteria=["do the thing"],
                                   assignee="recon-agent")
        wt_dir, branch = self._make_finished_branch(task_id)
        self._land_colliding_check_on_main()
        tip_before = base.run(["git", "rev-parse", branch], cwd=self.repo_path).stdout.strip()
        wt_real = os.path.realpath(wt_dir)

        result = spawn.resume(self.config, "reconproj", task_id, reconcile=True)
        name = self.track_session(result["session"])

        self.assertTrue(result["resumed"])
        self.assertTrue(result["reconcile"])
        self.assertEqual(result["agent"], "recon-agent")
        self.assertEqual(name, spawn.session_name("reconproj", task_id))
        base.wait_until(lambda: self._session_alive(name), timeout=10.0,
                         message="reconcile session never appeared")

        # task-133: the agent's resumeCmd ran (sleep 277) -- the same tier
        # a plain Resume picks -- NOT its own cmd (sleep 233), and the
        # resumed-framed reconcile prompt is the one trailing argument.
        pid = base.pane_pid(name)
        base.wait_until(lambda: any("277" in part for part in base.read_proc_cmdline(pid)), timeout=5.0,
                         message=f"pane wasn't running the agent's resumeCmd: {base.read_proc_cmdline(pid)}")
        cmdline = base.read_proc_cmdline(pid)
        self.assertFalse(any("233" in part for part in cmdline), cmdline)
        prompt = [part for part in cmdline if part.startswith("Reconcile backlog task")]
        self.assertEqual(len(prompt), 1, cmdline)
        self.assertEqual(cmdline[-1], prompt[0], cmdline)
        self.assertEqual(prompt[0], spawn.reconcile_prompt_for(task_id, "main", "sh check.sh", resumed=True))
        self.assertIn("You built this branch in this conversation", prompt[0])
        self.assertIn("`git merge main`", prompt[0])
        self.assertIn("`sh check.sh`", prompt[0])
        self.assertIn("Do NOT merge this branch into `main`", prompt[0])

        # Centrale merged/rebased nothing: same worktree, branch tip
        # unchanged, still exactly as far behind, worktree still clean.
        self.assertEqual(os.path.realpath(spawn.worktree_dir(self.config, "reconproj", task_id)), wt_real)
        tip_after = base.run(["git", "rev-parse", branch], cwd=self.repo_path).stdout.strip()
        self.assertEqual(tip_after, tip_before)
        self.assertEqual(self._behind_count(branch), 2)
        self.assertEqual(base.run(["git", "status", "--porcelain"], cwd=wt_dir).stdout.strip(), "")

        # Existing 409 rule: a live session refuses a second reconcile.
        with self.assertRaises(spawn.SpawnError) as ctx:
            spawn.resume(self.config, "reconproj", task_id, reconcile=True)
        self.assertEqual(ctx.exception.status, 409)


if __name__ == "__main__":
    unittest.main()
