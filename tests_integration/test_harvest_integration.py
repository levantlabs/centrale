"""Integration coverage for harvest.py's real merge flow: a temp repo
with a genuinely finished task branch (real git worktree, real commits,
real `backlog task edit`) merging through all five real gates, and a
conflicting branch getting refused at the real merge-clean gate.

These tests call harvest.py directly (not through spawn.py/tmux) since
harvesting only needs a worktree + branch to exist, not a live agent
session -- gate 1 explicitly requires *no* live session.

Run explicitly: python3 -m unittest tests_integration.test_harvest_integration
"""

from __future__ import annotations

import glob
import os
import sys
import unittest

from tests_integration import base

sys.path.insert(0, base.CENTRALE_ROOT)
import harvest  # noqa: E402
import spawn  # noqa: E402


def tearDownModule():
    base.assert_test_tmux_footprint_gone()


@base.require_tools("git", "backlog")
class HarvestIntegrationTests(base.IntegrationCase):
    def setUp(self):
        super().setUp()
        self.repo_path = base.init_backlog_repo(self.tmp_dir, name="harvestproj")
        self.worktree_root = os.path.join(self.tmp_dir, "worktrees")
        self.project = {"name": "harvestproj", "path": self.repo_path, "checkCommand": None}
        self.config = {
            "worktreeRoot": self.worktree_root,
            "projects": [self.project],
            "capabilities": {"tmux": True},
        }

    def _make_task_worktree(self, task_id):
        """Real git worktree + branch for task_id, mirroring what
        spawn._ensure_worktree does, without going through a tmux
        session (harvest gate 1 needs *no* live session).

        `backlog task create` leaves the new task file uncommitted (see
        base.create_task); a worktree is cut from a *commit*, not the
        working tree, so the task must be committed on main first or the
        worktree's own `backlog` CLI calls fail with "Task not found"
        (confirmed by hitting exactly that while writing this test) --
        the same ordering spawn.py's real _claim_and_commit enforces."""
        base.run(["git", "add", "-A"], cwd=self.repo_path)
        base.run(["git", "commit", "-q", "-m", f"add {task_id}"], cwd=self.repo_path)

        branch = spawn.branch_name(task_id)
        wt_dir = spawn.worktree_dir(self.config, "harvestproj", task_id)
        os.makedirs(self.worktree_root, exist_ok=True)
        base.run(["git", "worktree", "add", "-b", branch, wt_dir, "main"], cwd=self.repo_path)
        return wt_dir, branch

    def _finish_task_in_worktree(self, wt_dir, task_id, filename="work.txt", content="done\n"):
        """Real work + a real commit in the task's worktree, then marks
        the task Done with every AC checked via the real `backlog` CLI
        (which does not auto-commit -- confirmed by manual probing --
        so the commit below is required for gate 3 to see anything)."""
        with open(os.path.join(wt_dir, filename), "w", encoding="utf-8") as f:
            f.write(content)
        base.run(["backlog", "task", "edit", task_id, "-s", "Done", "--check-ac", "1", "--plain"], cwd=wt_dir)
        base.run(["git", "add", "-A"], cwd=wt_dir)
        base.run(["git", "commit", "-q", "-m", f"finish {task_id}"], cwd=wt_dir)

    def test_all_gates_pass_and_real_merge_happens(self):
        task_id = base.create_task(self.repo_path, "Finish me", acceptance_criteria=["do the thing"])
        wt_dir, branch = self._make_task_worktree(task_id)
        self._finish_task_in_worktree(wt_dir, task_id)

        self.project["checkCommand"] = "true"  # exercise gate 5 for real, not vacuously

        report = harvest.harvest_branch(self.config, "harvestproj", task_id)

        self.assertTrue(report["harvestable"], report["gates"])
        self.assertTrue(report["merged"], report)
        self.assertEqual(report["baseBranch"], "main")
        gate_status = {g["name"]: g["passed"] for g in report["gates"]}
        for name in harvest.GATE_NAMES:
            self.assertIs(gate_status[name], True, f"gate {name} did not pass: {report['gates']}")
        self.assertIs(gate_status.get("mainCheckoutClean"), True)

        # Real merge landed on main: worktree gone, branch gone, file present.
        self.assertFalse(os.path.isdir(wt_dir))
        branch_list = base.run(["git", "branch", "--list", branch], cwd=self.repo_path)
        self.assertEqual(branch_list.stdout.strip(), "")
        self.assertTrue(os.path.isfile(os.path.join(self.repo_path, "work.txt")))
        log = base.run(["git", "log", "-1", "--format=%s"], cwd=self.repo_path)
        self.assertIn(f"Merge {branch}", log.stdout)

    def test_conflicting_branch_fails_merge_clean_gate(self):
        # A file both the task branch and main will independently change,
        # so the real dry-run merge (gate 4) hits a genuine conflict.
        shared_path = os.path.join(self.repo_path, "shared.txt")
        with open(shared_path, "w", encoding="utf-8") as f:
            f.write("original\n")
        base.run(["git", "add", "-A"], cwd=self.repo_path)
        base.run(["git", "commit", "-q", "-m", "add shared.txt"], cwd=self.repo_path)

        task_id = base.create_task(self.repo_path, "Conflicted task", acceptance_criteria=["do the thing"])
        wt_dir, branch = self._make_task_worktree(task_id)

        # Diverge shared.txt on the task branch (staged/committed together
        # with the finishing work below, in one commit).
        with open(os.path.join(wt_dir, "shared.txt"), "w", encoding="utf-8") as f:
            f.write("changed on task branch\n")
        self._finish_task_in_worktree(wt_dir, task_id, filename="other.txt", content="also done\n")

        # ...and independently on main, in a conflicting way.
        with open(shared_path, "w", encoding="utf-8") as f:
            f.write("changed independently on main\n")
        base.run(["git", "add", "-A"], cwd=self.repo_path)
        base.run(["git", "commit", "-q", "-m", "diverge shared.txt on main"], cwd=self.repo_path)

        report = harvest.harvest_branch(self.config, "harvestproj", task_id)

        self.assertFalse(report["harvestable"])
        self.assertFalse(report["merged"])
        gate_status = {g["name"]: g["passed"] for g in report["gates"]}
        self.assertIs(gate_status["noLiveSession"], True)
        self.assertIs(gate_status["taskDone"], True)
        self.assertIs(gate_status["worktreeClean"], True)
        self.assertIs(gate_status["mergeClean"], False)
        self.assertIsNone(gate_status["checkCommand"])  # skipped: never reached

        # Nothing on main was touched by the failed dry-run merge -- it
        # only ever happens in a disposable scratch worktree.
        main_status = base.run(["git", "status", "--porcelain"], cwd=self.repo_path)
        self.assertEqual(main_status.stdout.strip(), "")
        with open(shared_path, encoding="utf-8") as f:
            self.assertEqual(f.read(), "changed independently on main\n")

        # The task's own worktree/branch are untouched (only the scratch
        # merge attempt failed, not anything real).
        self.assertTrue(os.path.isdir(wt_dir))
        branch_list = base.run(["git", "branch", "--list", branch], cwd=self.repo_path)
        self.assertNotEqual(branch_list.stdout.strip(), "")

    def _task_file_path(self, task_id):
        """The real, on-disk Backlog.md task file path for task_id --
        always spaced (e.g. "task-1 - Fix-the-login-bug.md": the CLI
        hyphenates within the title but always keeps literal spaces
        around the " - " separator), which is exactly the class of
        filename task-35 found the overlap check silently missing."""
        matches = glob.glob(os.path.join(self.repo_path, "backlog", "tasks", f"{task_id.lower()}*.md"))
        if not matches:
            raise AssertionError(f"no task file found on disk for {task_id}")
        return matches[0]

    def test_spaced_backlog_task_file_overlap_is_detected_and_refuses_politely(self):
        """Reproduces task-35's exact live failure: `git status
        --porcelain` quotes a path containing a space (which every
        Backlog.md task filename has -- the title alone is hyphenated,
        but the " - " separator before it is always literal spaces)
        while `git diff --name-only` emits the same path bare, so the
        overlap check silently never matched and let a doomed merge
        reach git's own real "would be overwritten" abort (a loud
        HarvestError) instead of a polite gate refusal naming the file.
        This asserts the fix: the pre-check itself catches it."""
        task_id = base.create_task(self.repo_path, "Fix the login bug", acceptance_criteria=["ac"])
        task_file = self._task_file_path(task_id)
        wt_dir, branch = self._make_task_worktree(task_id)
        # Marking the task Done edits (and commits, on the branch) this
        # exact task file -- the real, natural overlap.
        self._finish_task_in_worktree(wt_dir, task_id)

        # Uncommitted (unstaged) local edit to that SAME task file, on
        # main -- exactly what a stray `backlog task edit` run directly
        # against main, or a manual edit, would leave behind.
        with open(task_file, "a", encoding="utf-8") as f:
            f.write("\nstray uncommitted note\n")

        report = harvest.harvest_branch(self.config, "harvestproj", task_id)

        self.assertFalse(report["merged"], report)
        main_gate = next(g for g in report["gates"] if g["name"] == "mainCheckoutClean")
        self.assertFalse(main_gate["passed"])
        self.assertIn(os.path.basename(task_file), main_gate["reason"])
        self.assertIn("commit or stash", main_gate["reason"])
        # The stray edit is still there, untouched -- no merge attempt,
        # no abort cycle, nothing lost.
        with open(task_file, encoding="utf-8") as f:
            self.assertIn("stray uncommitted note", f.read())
        self.assertTrue(os.path.isdir(wt_dir))

    def test_unrelated_dirt_in_main_checkout_does_not_block_merge(self):
        """Task-34: git itself tolerates unrelated uncommitted dirt past
        a --no-ff merge -- the old "refuse on ANY uncommitted change"
        rule was stricter than git's real behavior. Empirically verified
        by hand before writing the fix, then encoded via
        harvest._parse_dirty_paths/_merge_touched_paths."""
        task_id = base.create_task(self.repo_path, "Fine to merge", acceptance_criteria=["ac"])
        wt_dir, branch = self._make_task_worktree(task_id)
        self._finish_task_in_worktree(wt_dir, task_id)  # touches work.txt only

        # Unrelated, uncommitted dirt in main's checkout -- a file the
        # merge never touches at all.
        unrelated_path = os.path.join(self.repo_path, "scratch-notes.txt")
        with open(unrelated_path, "w", encoding="utf-8") as f:
            f.write("unrelated local notes\n")

        report = harvest.harvest_branch(self.config, "harvestproj", task_id)

        self.assertTrue(report["merged"], report)
        self.assertEqual(report.get("unrelatedDirtyCount"), 1)
        # The unrelated file is left exactly as it was, still uncommitted.
        with open(unrelated_path, encoding="utf-8") as f:
            self.assertEqual(f.read(), "unrelated local notes\n")
        main_status = base.run(["git", "status", "--porcelain"], cwd=self.repo_path)
        self.assertIn("scratch-notes.txt", main_status.stdout)

    def test_overlapping_unstaged_dirt_refuses_with_the_file_list(self):
        shared_path = os.path.join(self.repo_path, "shared.txt")
        with open(shared_path, "w", encoding="utf-8") as f:
            f.write("original\n")
        base.run(["git", "add", "-A"], cwd=self.repo_path)
        base.run(["git", "commit", "-q", "-m", "add shared.txt"], cwd=self.repo_path)

        task_id = base.create_task(self.repo_path, "Touches shared", acceptance_criteria=["ac"])
        wt_dir, branch = self._make_task_worktree(task_id)
        with open(os.path.join(wt_dir, "shared.txt"), "w", encoding="utf-8") as f:
            f.write("changed on the branch\n")
        self._finish_task_in_worktree(wt_dir, task_id, filename="other.txt", content="also done\n")

        # Uncommitted (unstaged) local edit to the SAME file the branch
        # also changed -- must refuse and name it.
        with open(shared_path, "a", encoding="utf-8") as f:
            f.write("uncommitted local edit\n")

        report = harvest.harvest_branch(self.config, "harvestproj", task_id)

        self.assertFalse(report["merged"], report)
        main_gate = next(g for g in report["gates"] if g["name"] == "mainCheckoutClean")
        self.assertFalse(main_gate["passed"])
        self.assertIn("shared.txt", main_gate["reason"])
        self.assertIn("commit or stash", main_gate["reason"])
        # Nothing was merged -- the worktree/branch are both still there.
        self.assertTrue(os.path.isdir(wt_dir))
        branch_list = base.run(["git", "branch", "--list", branch], cwd=self.repo_path)
        self.assertNotEqual(branch_list.stdout.strip(), "")

    def test_untracked_file_colliding_with_branch_added_path_refuses(self):
        """The task-9 collision case: an untracked file already sitting
        at the exact path the branch would create."""
        task_id = base.create_task(self.repo_path, "Adds a new file", acceptance_criteria=["ac"])
        wt_dir, branch = self._make_task_worktree(task_id)
        self._finish_task_in_worktree(wt_dir, task_id, filename="work.txt", content="branch content\n")

        with open(os.path.join(self.repo_path, "work.txt"), "w", encoding="utf-8") as f:
            f.write("local untracked collision\n")

        report = harvest.harvest_branch(self.config, "harvestproj", task_id)

        self.assertFalse(report["merged"], report)
        main_gate = next(g for g in report["gates"] if g["name"] == "mainCheckoutClean")
        self.assertFalse(main_gate["passed"])
        self.assertIn("work.txt", main_gate["reason"])
        # The untracked file is left exactly as it was, never overwritten.
        with open(os.path.join(self.repo_path, "work.txt"), encoding="utf-8") as f:
            self.assertEqual(f.read(), "local untracked collision\n")

    def test_staged_change_refuses_even_when_unrelated_to_the_merge(self):
        """Documents a real, empirically-verified git behavior: `git
        merge --no-ff` refuses on ANY staged change, even to a path the
        incoming branch's own diff never touches at all -- confirmed by
        hand (a controlled git lab, not guessed) before writing
        harvest.py's fix. This is NOT an overlap-based refusal; it's
        git's own unconditional rule that it can't safely build a merge
        commit while the index disagrees with HEAD, regardless of which
        path is staged."""
        unrelated_path = os.path.join(self.repo_path, "unrelated.txt")
        with open(unrelated_path, "w", encoding="utf-8") as f:
            f.write("original\n")
        base.run(["git", "add", "-A"], cwd=self.repo_path)
        base.run(["git", "commit", "-q", "-m", "add unrelated.txt"], cwd=self.repo_path)

        task_id = base.create_task(self.repo_path, "Unrelated to staged file", acceptance_criteria=["ac"])
        wt_dir, branch = self._make_task_worktree(task_id)
        self._finish_task_in_worktree(wt_dir, task_id)  # touches work.txt only

        # A STAGED (git add'ed, not committed) change to unrelated.txt --
        # not part of the branch's diff at all.
        with open(unrelated_path, "a", encoding="utf-8") as f:
            f.write("staged local edit\n")
        base.run(["git", "add", "unrelated.txt"], cwd=self.repo_path)

        report = harvest.harvest_branch(self.config, "harvestproj", task_id)

        self.assertFalse(report["merged"], report)
        main_gate = next(g for g in report["gates"] if g["name"] == "mainCheckoutClean")
        self.assertFalse(main_gate["passed"])
        self.assertIn("unrelated.txt", main_gate["reason"])
        # Staged content untouched.
        staged_diff = base.run(["git", "diff", "--cached", "--name-only"], cwd=self.repo_path)
        self.assertIn("unrelated.txt", staged_diff.stdout)

    def test_evaluate_all_branches_read_only_never_merges(self):
        task_id = base.create_task(self.repo_path, "Untouched", acceptance_criteria=["ac"])
        wt_dir, branch = self._make_task_worktree(task_id)
        self._finish_task_in_worktree(wt_dir, task_id)

        reports = harvest.evaluate_all_branches(self.config, "harvestproj")

        self.assertEqual(len(reports), 1)
        self.assertTrue(reports[0]["harvestable"])
        # Still there -- GET-style evaluation never merges.
        self.assertTrue(os.path.isdir(wt_dir))
        branch_list = base.run(["git", "branch", "--list", branch], cwd=self.repo_path)
        self.assertNotEqual(branch_list.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
