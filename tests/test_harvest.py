import json
import re
import subprocess
import sys
import os
import threading
import time
import unittest
import urllib.error
import urllib.request
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import harvest  # noqa: E402
import server  # noqa: E402
import spawn  # noqa: E402


def make_config(worktree_root, projects):
    return {"port": 0, "worktreeRoot": worktree_root, "projects": projects}


def git_proc(args, returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(["git", *args], returncode, stdout, stderr)


def task_view(
    status="Done",
    ac=None,
    title="Do the thing",
    path="backlog/tasks/task-2 - Do the thing.md",
):
    ac = [{"index": 1, "text": "works", "checked": True}] if ac is None else ac
    return {
        "schemaVersion": 1,
        "kind": "task-view",
        "task": {
            "id": "TASK-2",
            "status": status,
            "acceptanceCriteria": ac,
            "title": title,
            "path": path,
        },
    }


class FakeGitHarvest:
    """Stateful git fake for harvest.py: routes by (cwd, subcommand),
    tracking which directory is currently a scratch worktree (created via
    `worktree add --detach`) so its status/merge calls can be answered
    distinctly from the real repo's or a spawn worktree's."""

    def __init__(
        self,
        repo_path,
        wt_dir,
        base_branch="main",
        task_branches=None,
        wt_status_dirty=False,
        repo_status_dirty=False,
        repo_dirty_porcelain=None,
        repo_diff_paths=None,
        repo_diff_fails=False,
        commits_over_main=1,
        scratch_worktree_add_fails=False,
        scratch_merge_conflict=False,
        real_merge_fails=False,
        worktree_remove_fails=False,
        branch_delete_fails=False,
        behind_base=0,
        behind_base_fails=False,
    ):
        self.repo_path = repo_path
        self.wt_dir = wt_dir
        self.base_branch = base_branch
        self.task_branches = task_branches or []
        self.wt_status_dirty = wt_status_dirty
        # repo_status_dirty is the simple on/off switch existing tests
        # use: a STAGED change ("M  dirty.py") -- an unconditional merge
        # blocker regardless of overlap (see harvest._parse_dirty_paths),
        # so it refuses without needing repo_diff_paths configured at
        # all. repo_dirty_porcelain gives new tests full control over
        # the exact porcelain lines (staged vs. unstaged vs. untracked,
        # which specific path) to exercise the overlap rule precisely.
        self.repo_status_dirty = repo_status_dirty
        self.repo_dirty_porcelain = repo_dirty_porcelain
        self.repo_diff_paths = repo_diff_paths
        self.repo_diff_fails = repo_diff_fails
        self.commits_over_main = commits_over_main
        self.scratch_worktree_add_fails = scratch_worktree_add_fails
        self.scratch_merge_conflict = scratch_merge_conflict
        self.real_merge_fails = real_merge_fails
        self.worktree_remove_fails = worktree_remove_fails
        self.branch_delete_fails = branch_delete_fails
        # task-66: `rev-list --count <branch>..<base>` (how far the branch
        # is BEHIND its base) is answered separately from gate 3's
        # `<base>..<branch>` (how far ahead), so a test can make a branch
        # behind without also changing whether it has anything to merge.
        self.behind_base = behind_base
        self.behind_base_fails = behind_base_fails
        self.calls = []
        self.scratch_dirs = set()

    def __call__(self, args, cwd=None):
        self.calls.append((list(args), cwd))

        if args[:1] == ["for-each-ref"]:
            return git_proc(args, 0, "\n".join(self.task_branches), "")

        if args[:1] == ["symbolic-ref"]:
            return git_proc(args, 0, self.base_branch + "\n", "")
        if args[:1] == ["rev-parse"] and "--abbrev-ref" in args:
            return git_proc(args, 0, self.base_branch + "\n", "")

        if args[:1] == ["status"]:
            if cwd == self.wt_dir:
                return git_proc(args, 0, " M dirty.py\n" if self.wt_status_dirty else "", "")
            if cwd == self.repo_path:
                if self.repo_dirty_porcelain is not None:
                    return git_proc(args, 0, self.repo_dirty_porcelain, "")
                return git_proc(args, 0, "M  dirty.py\n" if self.repo_status_dirty else "", "")
            return git_proc(args, 0, "", "")

        if args[:1] == ["diff"] and "--name-only" in args and cwd == self.repo_path:
            if self.repo_diff_fails:
                return git_proc(args, 1, "", "fatal: bad revision")
            paths = self.repo_diff_paths or []
            return git_proc(args, 0, ("\n".join(paths) + "\n") if paths else "", "")

        if args[:1] == ["rev-list"]:
            range_arg = args[-1]
            if range_arg.endswith(f"..{self.base_branch}"):
                if self.behind_base_fails:
                    return git_proc(args, 128, "", "fatal: bad revision")
                return git_proc(args, 0, f"{self.behind_base}\n", "")
            return git_proc(args, 0, f"{self.commits_over_main}\n", "")

        if args[:1] == ["worktree"] and args[1:2] == ["add"]:
            tmp_dir = args[-2]  # ["worktree", "add", "--detach", tmp_dir, base]
            self.scratch_dirs.add(tmp_dir)
            if self.scratch_worktree_add_fails:
                return git_proc(args, 128, "", "fatal: could not create scratch work tree")
            return git_proc(args, 0, "", "")

        if args[:1] == ["worktree"] and args[1:2] == ["remove"]:
            return git_proc(args, 0, "", "") if args[-1] in self.scratch_dirs or not self.worktree_remove_fails \
                else git_proc(args, 1, "", "fatal: could not remove worktree")

        if args[:1] == ["merge"] and "--abort" in args:
            return git_proc(args, 0, "", "")

        if args[:1] == ["merge"]:
            if cwd in self.scratch_dirs:
                if self.scratch_merge_conflict:
                    return git_proc(args, 1, "", "CONFLICT (content): merge conflict in file.py")
                return git_proc(args, 0, "", "")
            if cwd == self.repo_path:
                if self.real_merge_fails:
                    return git_proc(args, 1, "", "CONFLICT: unexpected real merge conflict")
                return git_proc(args, 0, "", "")
            return git_proc(args, 0, "", "")

        if args[:1] == ["branch"] and "-d" in args:
            if self.branch_delete_fails:
                return git_proc(args, 1, "", "error: branch not fully merged")
            return git_proc(args, 0, "", "")

        return git_proc(args, 0, "", "")


class DirtyPathParsingTests(unittest.TestCase):
    """Task-34: _parse_dirty_paths' staged/other split, verified against
    real `git status --porcelain` output captured empirically (see
    tests_integration/test_harvest_integration.py for the actual git
    runs that produced these exact lines)."""

    def test_untracked_file_is_other_not_staged(self):
        staged, other = harvest._parse_dirty_paths("?? new-file.txt\n")
        self.assertEqual(staged, set())
        self.assertEqual(other, {"new-file.txt"})

    def test_unstaged_modification_is_other_not_staged(self):
        staged, other = harvest._parse_dirty_paths(" M shared.txt\n")
        self.assertEqual(staged, set())
        self.assertEqual(other, {"shared.txt"})

    def test_staged_modification_is_staged(self):
        staged, other = harvest._parse_dirty_paths("M  shared.txt\n")
        self.assertEqual(staged, {"shared.txt"})
        self.assertEqual(other, set())

    def test_staged_new_file_is_staged(self):
        staged, other = harvest._parse_dirty_paths("A  new-staged.txt\n")
        self.assertEqual(staged, {"new-staged.txt"})
        self.assertEqual(other, set())

    def test_staged_and_further_modified_counts_as_both(self):
        # "MM": staged (index differs from HEAD) AND further modified in
        # the working tree since -- an unconditional blocker (staged)
        # that also happens to be reported as dirty either way.
        staged, other = harvest._parse_dirty_paths("MM shared.txt\n")
        self.assertEqual(staged, {"shared.txt"})
        self.assertEqual(other, {"shared.txt"})

    def test_rename_counts_both_old_and_new_path(self):
        staged, other = harvest._parse_dirty_paths("R  old-name.txt -> new-name.txt\n")
        self.assertEqual(staged, {"old-name.txt", "new-name.txt"})

    def test_multiple_lines_and_blank_lines(self):
        porcelain = " M shared.txt\n?? untracked.txt\nM  staged.txt\n\n"
        staged, other = harvest._parse_dirty_paths(porcelain)
        self.assertEqual(staged, {"staged.txt"})
        self.assertEqual(other, {"shared.txt", "untracked.txt"})

    def test_clean_status_yields_two_empty_sets(self):
        staged, other = harvest._parse_dirty_paths("")
        self.assertEqual(staged, set())
        self.assertEqual(other, set())

    def test_spaced_backlog_style_path_is_dequoted(self):
        # Task-35: git status --porcelain quotes a path the instant it
        # has a space -- every real Backlog.md task filename does
        # (around " - ") -- must come back as the real, unquoted path so
        # it can be compared against _merge_touched_paths' output.
        staged, other = harvest._parse_dirty_paths(
            '?? "backlog/tasks/task-1 - Fix the thing.md"\n'
        )
        self.assertEqual(other, {"backlog/tasks/task-1 - Fix the thing.md"})

    def test_quoted_rename_dequotes_both_sides(self):
        staged, other = harvest._parse_dirty_paths(
            'R  "old name.txt" -> "new name.txt"\n'
        )
        self.assertEqual(staged, {"old name.txt", "new name.txt"})


class UnstagedTrackedPathTests(unittest.TestCase):
    def test_only_plain_worktree_modifications_are_eligible(self):
        porcelain = (
            ' M "backlog/tasks/task-2 - Do the thing.md"\n'
            "?? untracked.txt\n"
            "M  staged.txt\n"
            "MM mixed.txt\n"
            " R old.txt -> renamed.txt\n"
        )
        self.assertEqual(
            harvest._unstaged_tracked_paths(porcelain),
            {"backlog/tasks/task-2 - Do the thing.md"},
        )


class MergeTouchedPathsTests(unittest.TestCase):
    """Task-34: _merge_touched_paths wraps `git diff --name-only`, with
    a real git failure returning None (distinct from "empty diff") so a
    caller unable to determine this never mistakes "couldn't check" for
    "safe to merge"."""

    def test_parses_touched_paths_from_diff_output(self):
        with mock.patch.object(server, "run_git", return_value=git_proc([], 0, "a.py\nb.py\n", "")) as run_git:
            result = harvest._merge_touched_paths("/repos/my-app", "main", "task/task-2")
        self.assertEqual(result, {"a.py", "b.py"})
        run_git.assert_called_once_with(["diff", "--name-only", "main..task/task-2"], cwd="/repos/my-app")

    def test_empty_diff_is_an_empty_set_not_none(self):
        with mock.patch.object(server, "run_git", return_value=git_proc([], 0, "", "")):
            result = harvest._merge_touched_paths("/repos/my-app", "main", "task/task-2")
        self.assertEqual(result, set())

    def test_git_failure_returns_none(self):
        with mock.patch.object(server, "run_git", return_value=git_proc([], 128, "", "fatal: bad revision")):
            result = harvest._merge_touched_paths("/repos/my-app", "main", "task/task-2")
        self.assertIsNone(result)

    def test_unquoted_spaced_path_from_diff_matches_the_dequoted_status_form(self):
        # Task-35's exact mismatch: git diff --name-only emits a spaced
        # path BARE (unlike status --porcelain, which quotes it) -- both
        # must dequote to the identical string for the overlap check
        # (_parse_dirty_paths) to ever intersect with this set at all.
        with mock.patch.object(server, "run_git", return_value=git_proc(
            [], 0, "backlog/tasks/task-1 - Fix the thing.md\n", "",
        )):
            result = harvest._merge_touched_paths("/repos/my-app", "main", "task/task-2")
        self.assertEqual(result, {"backlog/tasks/task-1 - Fix the thing.md"})


class HarvestHelperTests(unittest.TestCase):
    def test_task_id_from_branch(self):
        self.assertEqual(harvest.task_id_from_branch("task/task-2"), "TASK-2")
        self.assertEqual(harvest.task_id_from_branch("task/task-2.1"), "TASK-2.1")

    def test_safe_task_path_is_confined_to_the_exact_backlog_task(self):
        path = "backlog/tasks/task-2 - Do the thing.md"
        self.assertEqual(harvest._safe_task_path(path, "TASK-2"), path)
        self.assertIsNone(
            harvest._safe_task_path("backlog/tasks/task-3 - Other.md", "TASK-2")
        )
        self.assertIsNone(
            harvest._safe_task_path("../backlog/tasks/task-2 - Do the thing.md", "TASK-2")
        )
        self.assertIsNone(harvest._safe_task_path("/tmp/task-2.md", "TASK-2"))

    def test_task_id_from_branch_rejects_non_task_branches(self):
        self.assertIsNone(harvest.task_id_from_branch("main"))
        self.assertIsNone(harvest.task_id_from_branch("feature/foo"))
        self.assertIsNone(harvest.task_id_from_branch("task/not-a-task-id"))

    def test_list_task_branches(self):
        proc = git_proc([], 0, "task/task-2\ntask/task-3\n", "")
        with mock.patch.object(server, "run_git", return_value=proc):
            self.assertEqual(harvest.list_task_branches("/repos/my-app"), ["task/task-2", "task/task-3"])

    def test_list_task_branches_empty_on_failure(self):
        proc = git_proc([], 128, "", "fatal: not a git repository")
        with mock.patch.object(server, "run_git", return_value=proc):
            self.assertEqual(harvest.list_task_branches("/repos/my-app"), [])


class GateNoLiveSessionTests(unittest.TestCase):
    def test_passes_when_no_matching_session(self):
        with mock.patch.object(server, "list_sessions", return_value=[]):
            ok, reason = harvest._gate_no_live_session("my-app", "TASK-2")
        self.assertTrue(ok)
        self.assertIsNone(reason)

    def test_fails_when_session_still_live(self):
        sessions = [{"name": "centrale-my-app-task-2", "created": "0", "attached": False}]
        with mock.patch.object(server, "list_sessions", return_value=sessions):
            ok, reason = harvest._gate_no_live_session("my-app", "TASK-2")
        self.assertFalse(ok)
        self.assertIn("centrale-my-app-task-2", reason)

    def test_backlog_error_becomes_failure(self):
        with mock.patch.object(server, "list_sessions", side_effect=server.BacklogError("boom")):
            ok, reason = harvest._gate_no_live_session("my-app", "TASK-2")
        self.assertFalse(ok)
        self.assertIn("boom", reason)

    def test_fails_when_dotted_subtask_session_still_live(self):
        # task-59 AC #3: the merge gate must see the live session for a
        # dotted subtask id -- computed via the same spawn.session_name
        # tmux-safe encoding a real spawn used to create it, not a raw
        # "centrale-my-app-task-11.2" that was never the actual session
        # name (tmux itself rewrote the dot to "_" on creation).
        sessions = [{"name": "centrale-my-app-task-11_2", "created": "0", "attached": False}]
        with mock.patch.object(server, "list_sessions", return_value=sessions):
            ok, reason = harvest._gate_no_live_session("my-app", "TASK-11.2")
        self.assertFalse(ok)
        self.assertIn("centrale-my-app-task-11_2", reason)

    def test_passes_for_dotted_subtask_id_when_no_matching_session(self):
        with mock.patch.object(server, "list_sessions", return_value=[]):
            ok, reason = harvest._gate_no_live_session("my-app", "TASK-11.2")
        self.assertTrue(ok)
        self.assertIsNone(reason)


class GateTaskDoneTests(unittest.TestCase):
    def test_passes_when_done_and_all_ac_checked(self):
        with mock.patch.object(server, "run_backlog", return_value=task_view(status="Done")):
            ok, reason, task = harvest._gate_task_done("/repos/my-app", "TASK-2")
        self.assertTrue(ok)
        self.assertIsNone(reason)
        self.assertEqual(task["title"], "Do the thing")

    def test_fails_when_not_done(self):
        with mock.patch.object(server, "run_backlog", return_value=task_view(status="In Progress")):
            ok, reason, task = harvest._gate_task_done("/repos/my-app", "TASK-2")
        self.assertFalse(ok)
        self.assertIn("In Progress", reason)

    def test_fails_when_ac_unchecked(self):
        ac = [
            {"index": 1, "text": "a", "checked": True},
            {"index": 2, "text": "b", "checked": False},
        ]
        with mock.patch.object(server, "run_backlog", return_value=task_view(status="Done", ac=ac)):
            ok, reason, task = harvest._gate_task_done("/repos/my-app", "TASK-2")
        self.assertFalse(ok)
        self.assertIn("#2", reason)

    def test_backlog_error_becomes_failure(self):
        with mock.patch.object(server, "run_backlog", side_effect=server.BacklogError("nope")):
            ok, reason, task = harvest._gate_task_done("/repos/my-app", "TASK-2")
        self.assertFalse(ok)
        self.assertIsNone(task)


class GateTaskDoneFromBranchTests(unittest.TestCase):
    def setUp(self):
        self.project = {"name": "my-app", "path": "/repos/my-app"}

    def test_reads_spaced_task_path_through_backlog_in_detached_snapshot(self):
        spaced = "backlog/tasks/task-2 - Finished somewhere else.md"
        fake_git = FakeGitHarvest("/repos/my-app", "/worktrees/my-app-task-2")
        with mock.patch.object(server, "run_git", side_effect=fake_git), \
             mock.patch.object(
                 server,
                 "run_backlog",
                 return_value=task_view(status="Done", path=spaced),
             ) as run_backlog:
            ok, reason, task = harvest._gate_task_done_from_branch(
                self.project, "task/task-2", "TASK-2"
            )

        self.assertTrue(ok)
        self.assertIsNone(reason)
        self.assertEqual(task["path"], spaced)
        snapshot_cwd = run_backlog.call_args.kwargs["cwd"]
        self.assertIn(snapshot_cwd, fake_git.scratch_dirs)
        self.assertNotEqual(snapshot_cwd, "/worktrees/my-app-task-2")
        self.assertEqual(
            run_backlog.call_args.args[0],
            ["task", "view", "TASK-2", "--json"],
        )
        self.assertIn(
            (["worktree", "add", "--detach", snapshot_cwd, "task/task-2"], "/repos/my-app"),
            fake_git.calls,
        )
        self.assertIn(
            (["worktree", "remove", "--force", snapshot_cwd], "/repos/my-app"),
            fake_git.calls,
        )

    def test_snapshot_creation_failure_is_a_clean_task_gate_failure(self):
        fake_git = FakeGitHarvest(
            "/repos/my-app",
            "/worktrees/my-app-task-2",
            scratch_worktree_add_fails=True,
        )
        with mock.patch.object(server, "run_git", side_effect=fake_git), \
             mock.patch.object(server, "run_backlog") as run_backlog:
            ok, reason, task = harvest._gate_task_done_from_branch(
                self.project, "task/task-2", "TASK-2"
            )

        self.assertFalse(ok)
        self.assertIn("detached snapshot", reason)
        self.assertIsNone(task)
        run_backlog.assert_not_called()
        self.assertTrue(any(
            args[:2] == ["worktree", "remove"] for args, _cwd in fake_git.calls
        ))


class GateWorktreeCleanTests(unittest.TestCase):
    def setUp(self):
        self.config = make_config("/worktrees", [{"name": "my-app", "path": "/repos/my-app"}])
        self.project = {"name": "my-app", "path": "/repos/my-app"}
        self.wt_dir = "/worktrees/my-app-task-2"

    def test_passes_vacuously_when_branch_is_parked(self):
        fake_git = FakeGitHarvest(
            "/repos/my-app", self.wt_dir, commits_over_main=2, base_branch="main"
        )
        with mock.patch("os.path.isdir", return_value=False), \
             mock.patch.object(server, "run_git", side_effect=fake_git):
            ok, reason, wt_dir, base = harvest._gate_worktree_clean(
                self.config, self.project, "TASK-2", "task/task-2"
            )
        self.assertTrue(ok)
        self.assertEqual(reason, "no worktree -- nothing uncommitted to protect")
        self.assertIsNone(wt_dir)
        self.assertEqual(base, "main")

    def test_missing_worktree_fails_closed_when_git_cannot_list_checkouts(self):
        def run_git(args, cwd=None):
            if args[:2] == ["worktree", "list"]:
                return git_proc(args, 128, "", "fatal: worktree metadata unreadable")
            return git_proc(args, 0, "", "")

        with mock.patch("os.path.isdir", return_value=False), \
             mock.patch.object(server, "run_git", side_effect=run_git):
            ok, reason, wt_dir, base = harvest._gate_worktree_clean(
                self.config, self.project, "TASK-2", "task/task-2"
            )
        self.assertFalse(ok)
        self.assertIn("failed to list git worktrees", reason)
        self.assertIsNone(wt_dir)
        self.assertIsNone(base)

    def test_fails_when_worktree_dirty(self):
        fake_git = FakeGitHarvest("/repos/my-app", self.wt_dir, wt_status_dirty=True)
        with mock.patch("os.path.isdir", return_value=True), \
             mock.patch.object(server, "run_git", side_effect=fake_git):
            ok, reason, wt_dir, base = harvest._gate_worktree_clean(self.config, self.project, "TASK-2", "task/task-2")
        self.assertFalse(ok)
        self.assertIn("uncommitted", reason)

    def test_fails_cleanly_when_already_merged(self):
        # "nothing to merge" -- 0 commits over main.
        fake_git = FakeGitHarvest("/repos/my-app", self.wt_dir, commits_over_main=0)
        with mock.patch("os.path.isdir", return_value=True), \
             mock.patch.object(server, "run_git", side_effect=fake_git):
            ok, reason, wt_dir, base = harvest._gate_worktree_clean(self.config, self.project, "TASK-2", "task/task-2")
        self.assertFalse(ok)
        self.assertIn("nothing to merge", reason)

    def test_passes_when_clean_with_commits(self):
        fake_git = FakeGitHarvest("/repos/my-app", self.wt_dir, commits_over_main=2, base_branch="main")
        with mock.patch("os.path.isdir", return_value=True), \
             mock.patch.object(server, "run_git", side_effect=fake_git):
            ok, reason, wt_dir, base = harvest._gate_worktree_clean(self.config, self.project, "TASK-2", "task/task-2")
        self.assertTrue(ok)
        self.assertEqual(wt_dir, self.wt_dir)
        self.assertEqual(base, "main")


class GateWorktreeCleanExternalCheckoutTests(unittest.TestCase):
    """Task-70: gate 3's own missing-worktree reason is the same honest
    one gate 2 gives (it's only reachable in a race, but must never
    regress to the bare guess)."""

    def setUp(self):
        self.config = make_config("/worktrees", [{"name": "my-app", "path": "/repos/my-app"}])
        self.project = {"name": "my-app", "path": "/repos/my-app"}

    def test_missing_worktree_names_foreign_checkout(self):
        foreign = "/tmp/my-app-task-2-elsewhere"
        porcelain = f"worktree {foreign}\nHEAD bbb\nbranch refs/heads/task/task-2\n"

        def run_git(args, cwd=None):
            if args[:2] == ["worktree", "list"]:
                return git_proc(args, 0, porcelain, "")
            return git_proc(args, 0, "1700000000\n", "")

        with mock.patch("os.path.isdir", return_value=False), \
             mock.patch.object(server, "run_git", side_effect=run_git):
            ok, reason, wt_dir, base = harvest._gate_worktree_clean(
                self.config, self.project, "TASK-2", "task/task-2"
            )
        self.assertFalse(ok)
        self.assertIn(foreign, reason)
        self.assertNotIn("removed manually", reason)
        self.assertIsNone(wt_dir)
        self.assertIsNone(base)


class GateMergeAndCheckTests(unittest.TestCase):
    def setUp(self):
        self.project = {"name": "my-app", "path": "/repos/my-app"}

    def test_merge_conflict_fails_gate_and_skips_check(self):
        fake_git = FakeGitHarvest("/repos/my-app", "/worktrees/my-app-task-2", scratch_merge_conflict=True)
        with mock.patch.object(server, "run_git", side_effect=fake_git), \
             mock.patch.object(server, "run_check_command") as run_check:
            merge_ok, merge_reason, check_ok, check_reason = harvest._scratch_merge_and_check(
                self.project, "task/task-2", "main", "python3 -m unittest discover tests"
            )
        self.assertFalse(merge_ok)
        self.assertIn("conflict", merge_reason.lower())
        self.assertIsNone(check_ok)
        run_check.assert_not_called()
        # The scratch worktree must always be cleaned up, conflict or not.
        remove_calls = [c for c in fake_git.calls if c[0][:2] == ["worktree", "remove"]]
        self.assertEqual(len(remove_calls), 1)

    def test_clean_merge_with_no_check_command_passes_vacuously(self):
        fake_git = FakeGitHarvest("/repos/my-app", "/worktrees/my-app-task-2")
        with mock.patch.object(server, "run_git", side_effect=fake_git), \
             mock.patch.object(server, "run_check_command") as run_check:
            merge_ok, merge_reason, check_ok, check_reason = harvest._scratch_merge_and_check(
                self.project, "task/task-2", "main", None
            )
        self.assertTrue(merge_ok)
        self.assertTrue(check_ok)
        self.assertIsNone(check_reason)
        run_check.assert_not_called()

    def test_check_command_passes(self):
        fake_git = FakeGitHarvest("/repos/my-app", "/worktrees/my-app-task-2")
        with mock.patch.object(server, "run_git", side_effect=fake_git), \
             mock.patch.object(server, "run_check_command", return_value=git_proc([], 0, "OK", "")) as run_check:
            merge_ok, merge_reason, check_ok, check_reason = harvest._scratch_merge_and_check(
                self.project, "task/task-2", "main", "python3 -m unittest discover tests"
            )
        self.assertTrue(merge_ok)
        self.assertTrue(check_ok)
        run_check.assert_called_once()
        (command,), kwargs = run_check.call_args
        self.assertEqual(command, "python3 -m unittest discover tests")
        # Ran inside the scratch worktree, not the real repo.
        self.assertIn(kwargs["cwd"], fake_git.scratch_dirs)

    def test_check_command_fails(self):
        fake_git = FakeGitHarvest("/repos/my-app", "/worktrees/my-app-task-2")
        failing = git_proc([], 1, "", "FAILED (failures=1)")
        with mock.patch.object(server, "run_git", side_effect=fake_git), \
             mock.patch.object(server, "run_check_command", return_value=failing):
            merge_ok, merge_reason, check_ok, check_reason = harvest._scratch_merge_and_check(
                self.project, "task/task-2", "main", "python3 -m unittest discover tests"
            )
        self.assertTrue(merge_ok)
        self.assertFalse(check_ok)
        self.assertIn("checkCommand failed", check_reason)

    def test_check_command_uses_default_timeout_when_project_has_no_override(self):
        fake_git = FakeGitHarvest("/repos/my-app", "/worktrees/my-app-task-2")
        with mock.patch.object(server, "run_git", side_effect=fake_git), \
             mock.patch.object(server, "run_check_command", return_value=git_proc([], 0, "", "")) as run_check:
            harvest._scratch_merge_and_check(self.project, "task/task-2", "main", "make test")
        self.assertEqual(run_check.call_args.kwargs["timeout"], server.DEFAULT_CHECK_TIMEOUT_SECONDS)

    def test_check_command_uses_projects_checkTimeoutSeconds_override(self):
        project = {"name": "my-app", "path": "/repos/my-app", "checkTimeoutSeconds": 45}
        fake_git = FakeGitHarvest("/repos/my-app", "/worktrees/my-app-task-2")
        with mock.patch.object(server, "run_git", side_effect=fake_git), \
             mock.patch.object(server, "run_check_command", return_value=git_proc([], 0, "", "")) as run_check:
            harvest._scratch_merge_and_check(project, "task/task-2", "main", "make test")
        self.assertEqual(run_check.call_args.kwargs["timeout"], 45)

    def test_check_command_timeout_is_a_clear_gate_failure_not_a_generic_one(self):
        # _run() synthesizes returncode 124 for a subprocess.TimeoutExpired
        # (see server.py) -- a hung checkCommand must read as a distinct,
        # clear timeout message, not just "checkCommand failed".
        fake_git = FakeGitHarvest("/repos/my-app", "/worktrees/my-app-task-2")
        timed_out = git_proc([], 124, "", "Command '...' timed out after 600 seconds")
        with mock.patch.object(server, "run_git", side_effect=fake_git), \
             mock.patch.object(server, "run_check_command", return_value=timed_out):
            merge_ok, merge_reason, check_ok, check_reason = harvest._scratch_merge_and_check(
                self.project, "task/task-2", "main", "make test"
            )
        self.assertTrue(merge_ok)
        self.assertFalse(check_ok)
        self.assertIn("timed out", check_reason)
        self.assertIn(str(server.DEFAULT_CHECK_TIMEOUT_SECONDS), check_reason)

    def test_scratch_worktree_add_failure_is_a_clean_gate_failure(self):
        fake_git = FakeGitHarvest("/repos/my-app", "/worktrees/my-app-task-2", scratch_worktree_add_fails=True)
        with mock.patch.object(server, "run_git", side_effect=fake_git):
            merge_ok, merge_reason, check_ok, check_reason = harvest._scratch_merge_and_check(
                self.project, "task/task-2", "main", None
            )
        self.assertFalse(merge_ok)
        self.assertIn("scratch worktree", merge_reason)


class EvaluateBranchTests(unittest.TestCase):
    def setUp(self):
        self.config = make_config("/worktrees", [{"name": "my-app", "path": "/repos/my-app"}])
        self.project = {"name": "my-app", "path": "/repos/my-app"}
        self.wt_dir = "/worktrees/my-app-task-2"

    def test_all_gates_pass(self):
        fake_git = FakeGitHarvest("/repos/my-app", self.wt_dir, commits_over_main=1)
        with mock.patch.object(server, "list_sessions", return_value=[]), \
             mock.patch.object(server, "run_backlog", return_value=task_view(status="Done")), \
             mock.patch("os.path.isdir", return_value=True), \
             mock.patch.object(server, "run_git", side_effect=fake_git):
            report = harvest.evaluate_branch(self.config, self.project, "TASK-2")
        self.assertTrue(report["harvestable"])
        self.assertEqual([g["name"] for g in report["gates"]], harvest.GATE_NAMES)
        self.assertTrue(all(g["passed"] for g in report["gates"]))
        self.assertEqual(report["taskTitle"], "Do the thing")
        self.assertEqual(report["branch"], "task/task-2")

    def test_first_gate_failure_skips_the_rest(self):
        sessions = [{"name": "centrale-my-app-task-2", "created": "0", "attached": False}]
        with mock.patch.object(server, "list_sessions", return_value=sessions), \
             mock.patch.object(server, "run_backlog") as run_backlog, \
             mock.patch.object(server, "run_git", return_value=git_proc([], 0, "", "")) as run_git:
            report = harvest.evaluate_branch(self.config, self.project, "TASK-2")
        self.assertFalse(report["harvestable"])
        self.assertEqual(report["gates"][0]["name"], "noLiveSession")
        self.assertFalse(report["gates"][0]["passed"])
        for g in report["gates"][1:]:
            self.assertIsNone(g["passed"])
        # No later gate did any real work -- the only git call at all is
        # the branch-existence pre-check ahead of every gate.
        run_backlog.assert_not_called()
        run_git.assert_called_once()
        self.assertEqual(run_git.call_args.args[0][0], "rev-parse")

    def test_task_not_done_skips_worktree_merge_and_check_gates(self):
        with mock.patch.object(server, "list_sessions", return_value=[]), \
             mock.patch.object(server, "run_backlog", return_value=task_view(status="To Do")) as run_backlog, \
             mock.patch("os.path.isdir", return_value=True), \
             mock.patch.object(server, "run_git", return_value=git_proc([], 0, "", "")) as run_git:
            report = harvest.evaluate_branch(self.config, self.project, "TASK-2")
        self.assertFalse(report["harvestable"])
        self.assertEqual(report["gates"][1]["name"], "taskDone")
        self.assertFalse(report["gates"][1]["passed"])
        self.assertIn("To Do", report["gates"][1]["reason"])
        for g in report["gates"][2:]:
            self.assertIsNone(g["passed"])
        # Only the branch-existence pre-check calls git; nothing past
        # gate 2 does.
        run_git.assert_called_once()
        self.assertEqual(run_git.call_args.args[0][0], "rev-parse")
        # Gate 2 reads task state from the branch's own worktree, not
        # main -- an agent's Done status only exists there until merged.
        self.assertEqual(run_backlog.call_count, 2)
        run_backlog.assert_any_call(
            ["task", "view", "TASK-2", "--json"], cwd=self.wt_dir
        )
        run_backlog.assert_any_call(
            ["task", "view", "TASK-2", "--json"], cwd="/repos/my-app"
        )
        self.assertEqual(
            report["gates"][1]["reason"], "task status is 'To Do', not Done"
        )

    def test_main_done_divergence_names_both_sides_and_last_branch_commit(self):
        task_path = "backlog/tasks/task-2 - Do the thing.md"

        def read_task(args, cwd):
            if cwd == self.wt_dir:
                return task_view(status="In Progress", path=task_path)
            return task_view(status="Done", path=task_path)

        def run_git(args, cwd=None):
            if args[:1] == ["status"]:
                return git_proc(args, 0, f' M "{task_path}"\n', "")
            if args[:1] == ["log"]:
                return git_proc(args, 0, "agent: leave operator step pending\n", "")
            return git_proc(args, 0, "", "")

        with mock.patch.object(server, "list_sessions", return_value=[]), \
             mock.patch.object(server, "run_backlog", side_effect=read_task), \
             mock.patch("os.path.isdir", return_value=True), \
             mock.patch.object(server, "run_git", side_effect=run_git):
            report = harvest.evaluate_branch(self.config, self.project, "TASK-2")

        self.assertFalse(report["harvestable"])
        gate = report["gates"][1]
        self.assertEqual(gate["name"], "taskDone")
        self.assertFalse(gate["passed"])
        self.assertIn("agent branch status is 'In Progress'", gate["reason"])
        self.assertIn("board/main checkout", gate["reason"])
        self.assertIn("uncommitted edit", gate["reason"])
        self.assertEqual(report["doneDivergence"], {
            "branchStatus": "In Progress",
            "boardStatus": "Done",
            "branchTaskPath": task_path,
            "mainTaskPath": task_path,
            "mainTaskUncommitted": True,
            "lastBranchCommitSubject": "agent: leave operator step pending",
        })

    def test_task_done_gate_reads_parked_branch_and_reports_branch_status(self):
        spaced = "backlog/tasks/task-2 - Finished somewhere else.md"
        fake_git = FakeGitHarvest("/repos/my-app", self.wt_dir)

        # Both the detached branch snapshot and main say In Progress, so
        # the ordinary branch-side status reason is preserved without a
        # Done-divergence annotation.
        with mock.patch.object(server, "list_sessions", return_value=[]), \
             mock.patch.object(
                 server,
                 "run_backlog",
                 return_value=task_view(status="In Progress", path=spaced),
             ) as run_backlog, \
             mock.patch("os.path.isdir", return_value=False), \
             mock.patch.object(server, "run_git", side_effect=fake_git):
            report = harvest.evaluate_branch(self.config, self.project, "TASK-2")

        self.assertFalse(report["harvestable"])
        self.assertEqual(report["gates"][1]["name"], "taskDone")
        self.assertFalse(report["gates"][1]["passed"])
        self.assertEqual(
            report["gates"][1]["reason"],
            "task status is 'In Progress', not Done",
        )
        self.assertEqual(report["taskPath"], spaced)
        self.assertEqual(report["branchCheckout"], {"kind": "none", "path": None})
        for g in report["gates"][2:]:
            self.assertIsNone(g["passed"])
        self.assertEqual(run_backlog.call_count, 2)
        branch_read = run_backlog.call_args_list[0]
        self.assertIn(branch_read.kwargs["cwd"], fake_git.scratch_dirs)
        self.assertNotEqual(branch_read.kwargs["cwd"], self.wt_dir)
        run_backlog.assert_any_call(
            ["task", "view", "TASK-2", "--json"], cwd="/repos/my-app"
        )

    def _external_git(self, checkout_path):
        porcelain = (
            "worktree /repos/my-app\nHEAD aaa\nbranch refs/heads/main\n\n"
            f"worktree {checkout_path}\nHEAD bbb\nbranch refs/heads/task/task-2\n"
        )

        def run_git(args, cwd=None):
            if args[:2] == ["worktree", "list"]:
                return git_proc(args, 0, porcelain, "")
            if args[:1] == ["log"]:
                return git_proc(args, 0, "1700000000\n", "")
            return git_proc(args, 0, "", "")
        return run_git

    def test_task_done_gate_names_the_foreign_checkout_instead_of_guessing(self):
        # task-70 AC #3: a branch adopted into a worktree Centrale doesn't
        # manage -- the reason names that path, never "removed manually?".
        foreign = "/repos/my-app/.worktrees/task-2-integrated"
        with mock.patch.object(server, "list_sessions", return_value=[]), \
             mock.patch.object(server, "run_backlog") as run_backlog, \
             mock.patch("os.path.isdir", return_value=False), \
             mock.patch.object(server, "run_git", side_effect=self._external_git(foreign)):
            report = harvest.evaluate_branch(self.config, self.project, "TASK-2")
        self.assertFalse(report["harvestable"])
        gate = report["gates"][1]
        self.assertEqual(gate["name"], "taskDone")
        self.assertFalse(gate["passed"])
        self.assertIn(foreign, gate["reason"])
        self.assertIn("outside Centrale", gate["reason"])
        self.assertNotIn("removed manually", gate["reason"])
        self.assertEqual(report["branchCheckout"]["kind"], "external")
        self.assertEqual(report["branchCheckout"]["path"], foreign)
        self.assertEqual(report["branchCheckout"]["lastCommitAt"], "2023-11-14T22:13:20Z")
        for g in report["gates"][2:]:
            self.assertIsNone(g["passed"])
        run_backlog.assert_not_called()

    def test_task_done_gate_keeps_manual_removal_guess_for_a_stale_centrale_registration(self):
        # git still lists the branch at Centrale's own path but the
        # directory is gone (rm -rf without `git worktree remove`).
        with mock.patch.object(server, "list_sessions", return_value=[]), \
             mock.patch.object(server, "run_backlog"), \
             mock.patch("os.path.isdir", return_value=False), \
             mock.patch.object(server, "run_git", side_effect=self._external_git(self.wt_dir)):
            report = harvest.evaluate_branch(self.config, self.project, "TASK-2")
        reason = report["gates"][1]["reason"]
        self.assertIn("was it removed manually?", reason)
        self.assertIn(self.wt_dir, reason)
        self.assertNotIn("outside Centrale", reason)
        self.assertEqual(report["branchCheckout"], {"kind": "centrale", "path": self.wt_dir})

    def test_branch_checkout_absent_from_report_when_worktree_exists(self):
        fake_git = FakeGitHarvest("/repos/my-app", self.wt_dir)
        with mock.patch.object(server, "list_sessions", return_value=[]), \
             mock.patch.object(server, "run_backlog", return_value=task_view(status="Done")), \
             mock.patch("os.path.isdir", return_value=True), \
             mock.patch.object(server, "run_git", side_effect=fake_git):
            report = harvest.evaluate_branch(self.config, self.project, "TASK-2")
        self.assertNotIn("branchCheckout", report)

    def test_merge_conflict_skips_check_command_gate_only(self):
        fake_git = FakeGitHarvest("/repos/my-app", self.wt_dir, scratch_merge_conflict=True)
        with mock.patch.object(server, "list_sessions", return_value=[]), \
             mock.patch.object(server, "run_backlog", return_value=task_view(status="Done")), \
             mock.patch("os.path.isdir", return_value=True), \
             mock.patch.object(server, "run_git", side_effect=fake_git):
            report = harvest.evaluate_branch(self.config, self.project, "TASK-2")
        self.assertFalse(report["harvestable"])
        names = [g["name"] for g in report["gates"]]
        self.assertEqual(names, harvest.GATE_NAMES)
        self.assertFalse(report["gates"][3]["passed"])
        self.assertIsNone(report["gates"][4]["passed"])

    def test_missing_branch_reports_already_merged_not_a_gate_failure(self):
        # rev-parse --verify --quiet fails for a branch that doesn't
        # exist -- distinct from "worktree not found": there's nothing
        # here to evaluate at all (already merged, or never existed).
        with mock.patch.object(server, "list_sessions") as list_sessions, \
             mock.patch.object(server, "run_backlog") as run_backlog, \
             mock.patch.object(server, "run_git", return_value=git_proc([], 1, "", "")) as run_git:
            report = harvest.evaluate_branch(self.config, self.project, "TASK-2")

        self.assertFalse(report["harvestable"])
        self.assertTrue(report.get("alreadyMerged"))
        self.assertEqual(report["gates"], [])
        self.assertIsNone(report["taskTitle"])
        self.assertEqual(report["branch"], "task/task-2")
        # No gate evaluation work happens at all -- not even gate 1.
        list_sessions.assert_not_called()
        run_backlog.assert_not_called()
        run_git.assert_called_once_with(
            ["rev-parse", "--verify", "--quiet", "refs/heads/task/task-2"], cwd="/repos/my-app"
        )


class BehindBaseCountTests(unittest.TestCase):
    """task-66: the behind-count is derived from git at evaluation time
    (`rev-list --count <branch>..<base>`, i.e. commits on the base not
    reachable from the branch, via their merge-base); nothing is stored."""

    def test_counts_commits_on_base_not_reachable_from_branch(self):
        fake_git = FakeGitHarvest("/repos/my-app", "/worktrees/my-app-task-2", behind_base=3)
        with mock.patch.object(server, "run_git", side_effect=fake_git):
            count = harvest._behind_base_count("/repos/my-app", "main", "task/task-2")
        self.assertEqual(count, 3)
        (args, cwd), = fake_git.calls
        self.assertEqual(args, ["rev-list", "--count", "task/task-2..main"])
        self.assertEqual(cwd, "/repos/my-app")

    def test_zero_when_branch_contains_everything_on_base(self):
        fake_git = FakeGitHarvest("/repos/my-app", "/worktrees/my-app-task-2", behind_base=0)
        with mock.patch.object(server, "run_git", side_effect=fake_git):
            self.assertEqual(harvest._behind_base_count("/repos/my-app", "main", "task/task-2"), 0)

    def test_none_on_git_failure_or_garbage(self):
        fake_git = FakeGitHarvest("/repos/my-app", "/worktrees/my-app-task-2", behind_base_fails=True)
        with mock.patch.object(server, "run_git", side_effect=fake_git):
            self.assertIsNone(harvest._behind_base_count("/repos/my-app", "main", "task/task-2"))
        with mock.patch.object(server, "run_git", return_value=git_proc([], 0, "not-a-number\n", "")):
            self.assertIsNone(harvest._behind_base_count("/repos/my-app", "main", "task/task-2"))

    def test_hint_wording_names_count_and_base(self):
        self.assertEqual(
            harvest._reconcile_hint("main", 1),
            "this branch predates 1 newer commit on main; the failure may be a "
            "collision with newer work, not a defect in the branch",
        )
        self.assertIn("4 newer commits on develop", harvest._reconcile_hint("develop", 4))


class BehindBaseHintGatingTests(unittest.TestCase):
    """task-66: behindBase + reconcileHint appear on a report ONLY when
    the branch is behind its base AND the failing gate is one whose
    outcome depends on the base (mergeClean / checkCommand). Every other
    report -- passing, failing earlier, up to date, or undeterminable --
    carries neither key, so the frontend has one signal to key off."""

    def setUp(self):
        self.config = make_config("/worktrees", [{"name": "my-app", "path": "/repos/my-app"}])
        self.project = {
            "name": "my-app", "path": "/repos/my-app",
            "checkCommand": "python3 -m unittest discover tests",
        }
        self.wt_dir = "/worktrees/my-app-task-2"

    def _evaluate(self, fake_git, task_status="Done", check_proc=None, sessions=None):
        check_proc = check_proc if check_proc is not None else git_proc([], 0, "OK", "")
        with mock.patch.object(server, "list_sessions", return_value=sessions or []), \
             mock.patch.object(server, "run_backlog", return_value=task_view(status=task_status)), \
             mock.patch("os.path.isdir", return_value=True), \
             mock.patch.object(server, "run_check_command", return_value=check_proc), \
             mock.patch.object(server, "run_git", side_effect=fake_git):
            return harvest.evaluate_branch(self.config, self.project, "TASK-2")

    def _behind_calls(self, fake_git):
        return [c for c in fake_git.calls if c[0][:2] == ["rev-list", "--count"] and c[0][-1].endswith("..main")]

    def test_check_failure_on_a_behind_branch_carries_count_and_hint(self):
        fake_git = FakeGitHarvest("/repos/my-app", self.wt_dir, behind_base=2)
        failing = git_proc([], 1, "", "FAILED (failures=1)")
        report = self._evaluate(fake_git, check_proc=failing)
        self.assertFalse(report["harvestable"])
        self.assertFalse(report["gates"][4]["passed"])
        self.assertIn("checkCommand failed", report["gates"][4]["reason"])  # existing reason intact
        self.assertEqual(report["behindBase"], {"baseBranch": "main", "count": 2})
        self.assertEqual(
            report["reconcileHint"],
            "this branch predates 2 newer commits on main; the failure may be a "
            "collision with newer work, not a defect in the branch",
        )

    def test_merge_conflict_on_a_behind_branch_carries_count_and_hint(self):
        fake_git = FakeGitHarvest("/repos/my-app", self.wt_dir, behind_base=1, scratch_merge_conflict=True)
        report = self._evaluate(fake_git)
        self.assertFalse(report["gates"][3]["passed"])
        self.assertEqual(report["behindBase"], {"baseBranch": "main", "count": 1})
        self.assertIn("1 newer commit on main", report["reconcileHint"])

    def test_up_to_date_failing_branch_has_no_hint(self):
        fake_git = FakeGitHarvest("/repos/my-app", self.wt_dir, behind_base=0)
        failing = git_proc([], 1, "", "FAILED (failures=1)")
        report = self._evaluate(fake_git, check_proc=failing)
        self.assertFalse(report["harvestable"])
        self.assertNotIn("behindBase", report)
        self.assertNotIn("reconcileHint", report)
        self.assertEqual(len(self._behind_calls(fake_git)), 1)  # derived, found 0, omitted

    def test_undeterminable_behind_count_yields_no_hint(self):
        fake_git = FakeGitHarvest("/repos/my-app", self.wt_dir, behind_base_fails=True)
        failing = git_proc([], 1, "", "FAILED (failures=1)")
        report = self._evaluate(fake_git, check_proc=failing)
        self.assertNotIn("behindBase", report)
        self.assertNotIn("reconcileHint", report)

    def test_passing_branch_never_carries_hint_even_when_behind(self):
        fake_git = FakeGitHarvest("/repos/my-app", self.wt_dir, behind_base=5)
        report = self._evaluate(fake_git)
        self.assertTrue(report["harvestable"])
        self.assertNotIn("behindBase", report)
        self.assertNotIn("reconcileHint", report)
        # Not even derived: the passing path costs no extra git call.
        self.assertEqual(self._behind_calls(fake_git), [])

    def test_earlier_gate_failures_never_carry_hint_even_when_behind(self):
        # Gate 1: live session.
        fake_git = FakeGitHarvest("/repos/my-app", self.wt_dir, behind_base=5)
        sessions = [{"name": "centrale-my-app-task-2", "created": "0", "attached": False}]
        report = self._evaluate(fake_git, sessions=sessions)
        self.assertFalse(report["gates"][0]["passed"])
        self.assertNotIn("behindBase", report)
        self.assertEqual(self._behind_calls(fake_git), [])
        # Gate 2: not Done.
        fake_git = FakeGitHarvest("/repos/my-app", self.wt_dir, behind_base=5)
        report = self._evaluate(fake_git, task_status="In Progress")
        self.assertFalse(report["gates"][1]["passed"])
        self.assertNotIn("behindBase", report)
        self.assertEqual(self._behind_calls(fake_git), [])
        # Gate 3: dirty worktree.
        fake_git = FakeGitHarvest("/repos/my-app", self.wt_dir, behind_base=5, wt_status_dirty=True)
        report = self._evaluate(fake_git)
        self.assertFalse(report["gates"][2]["passed"])
        self.assertNotIn("behindBase", report)
        self.assertEqual(self._behind_calls(fake_git), [])

    def test_hint_rides_through_a_blocked_post_harvest_report(self):
        # The drawer only ever sees the report POST /api/harvest returns
        # for a blocked merge -- the fields must survive harvest_branch's
        # own wrapping, and Centrale must still merge nothing.
        fake_git = FakeGitHarvest("/repos/my-app", self.wt_dir, behind_base=2)
        failing = git_proc([], 1, "", "FAILED (failures=1)")
        config = make_config("/worktrees", [self.project])
        with mock.patch.object(server, "list_sessions", return_value=[]), \
             mock.patch.object(server, "run_backlog", return_value=task_view(status="Done")), \
             mock.patch("os.path.isdir", return_value=True), \
             mock.patch.object(server, "run_check_command", return_value=failing), \
             mock.patch.object(server, "run_git", side_effect=fake_git):
            report = harvest.harvest_branch(config, "my-app", "TASK-2")
        self.assertFalse(report["merged"])
        self.assertEqual(report["behindBase"]["count"], 2)
        self.assertIn("reconcileHint", report)
        self.assertFalse(any(c[0][:1] == ["merge"] and c[1] == "/repos/my-app" for c in fake_git.calls))


class HarvestBranchTests(unittest.TestCase):
    def setUp(self):
        self.config = make_config("/worktrees", [{"name": "my-app", "path": "/repos/my-app"}])
        self.wt_dir = "/worktrees/my-app-task-2"
        harvest._reset_events()
        self.addCleanup(harvest._reset_events)

    def _all_green_git(self, **kwargs):
        kwargs.setdefault("commits_over_main", 1)
        return FakeGitHarvest("/repos/my-app", self.wt_dir, **kwargs)

    def test_merges_and_cleans_up_when_all_gates_pass(self):
        fake_git = self._all_green_git()
        with mock.patch.object(server, "list_sessions", return_value=[]), \
             mock.patch.object(server, "run_backlog", return_value=task_view(status="Done", title="Fix the bug")), \
             mock.patch("os.path.isdir", return_value=True), \
             mock.patch.object(server, "run_git", side_effect=fake_git):
            report = harvest.harvest_branch(self.config, "my-app", "TASK-2")

        self.assertTrue(report["merged"])
        self.assertNotIn("warnings", report)

        real_merge_calls = [c for c in fake_git.calls if c[0][:1] == ["merge"] and c[1] == "/repos/my-app"]
        self.assertEqual(len(real_merge_calls), 1)
        self.assertIn("-m", real_merge_calls[0][0])
        msg = real_merge_calls[0][0][real_merge_calls[0][0].index("-m") + 1]
        self.assertEqual(msg, "Merge task/task-2: Fix the bug")

        remove_calls = [c for c in fake_git.calls if c[0][:2] == ["worktree", "remove"] and c[1] == "/repos/my-app"]
        self.assertTrue(any(c[0][-1] == self.wt_dir for c in remove_calls))

        branch_del_calls = [c for c in fake_git.calls if c[0][:1] == ["branch"] and "-d" in c[0]]
        self.assertEqual(len(branch_del_calls), 1)

    def test_gate_failure_never_merges_or_touches_anything(self):
        sessions = [{"name": "centrale-my-app-task-2", "created": "0", "attached": False}]
        with mock.patch.object(server, "list_sessions", return_value=sessions), \
             mock.patch.object(server, "run_backlog") as run_backlog, \
             mock.patch.object(server, "run_git", return_value=git_proc([], 0, "", "")) as run_git:
            report = harvest.harvest_branch(self.config, "my-app", "TASK-2")

        self.assertFalse(report["merged"])
        self.assertFalse(report["harvestable"])
        run_backlog.assert_not_called()
        # Only the branch-existence pre-check calls git; no gate past it does.
        run_git.assert_called_once()
        self.assertEqual(run_git.call_args.args[0][0], "rev-parse")

    def test_dirty_main_checkout_refuses_without_stash_or_force(self):
        fake_git = self._all_green_git(repo_status_dirty=True)
        with mock.patch.object(server, "list_sessions", return_value=[]), \
             mock.patch.object(server, "run_backlog", return_value=task_view(status="Done")), \
             mock.patch("os.path.isdir", return_value=True), \
             mock.patch.object(server, "run_git", side_effect=fake_git):
            report = harvest.harvest_branch(self.config, "my-app", "TASK-2")

        self.assertFalse(report["merged"])
        main_gate = next(g for g in report["gates"] if g["name"] == "mainCheckoutClean")
        self.assertFalse(main_gate["passed"])
        self.assertIn("uncommitted", main_gate["reason"])
        # Never stashed, never forced a merge over dirty state.
        self.assertFalse(any("stash" in c[0] for c in fake_git.calls))
        real_merge_calls = [c for c in fake_git.calls if c[0][:1] == ["merge"] and c[1] == "/repos/my-app"]
        self.assertEqual(real_merge_calls, [])

    def test_unrelated_unstaged_dirt_merges_fine_and_is_noted_on_the_report(self):
        # Main has an unstaged modification to a file the merge never
        # touches -- empirically confirmed real git tolerates this (see
        # tests_integration/test_harvest_integration.py); must not block.
        fake_git = self._all_green_git(
            repo_dirty_porcelain=" M unrelated.txt\n",
            repo_diff_paths=["shared.txt", "branch-new-file.txt"],
        )
        with mock.patch.object(server, "list_sessions", return_value=[]), \
             mock.patch.object(server, "run_backlog", return_value=task_view(status="Done")), \
             mock.patch("os.path.isdir", return_value=True), \
             mock.patch.object(server, "run_git", side_effect=fake_git):
            report = harvest.harvest_branch(self.config, "my-app", "TASK-2")

        self.assertTrue(report["merged"])
        main_gate = next(g for g in report["gates"] if g["name"] == "mainCheckoutClean")
        self.assertTrue(main_gate["passed"])
        self.assertEqual(report["unrelatedDirtyCount"], 1)
        real_merge_calls = [c for c in fake_git.calls if c[0][:1] == ["merge"] and c[1] == "/repos/my-app"]
        self.assertEqual(len(real_merge_calls), 1)

    def test_overlapping_unstaged_dirt_refuses_and_lists_the_file(self):
        fake_git = self._all_green_git(
            repo_dirty_porcelain=" M shared.txt\n",
            repo_diff_paths=["shared.txt", "branch-new-file.txt"],
        )
        with mock.patch.object(server, "list_sessions", return_value=[]), \
             mock.patch.object(server, "run_backlog", return_value=task_view(status="Done")), \
             mock.patch("os.path.isdir", return_value=True), \
             mock.patch.object(server, "run_git", side_effect=fake_git):
            report = harvest.harvest_branch(self.config, "my-app", "TASK-2")

        self.assertFalse(report["merged"])
        main_gate = next(g for g in report["gates"] if g["name"] == "mainCheckoutClean")
        self.assertFalse(main_gate["passed"])
        self.assertIn("shared.txt", main_gate["reason"])
        self.assertIn("commit or stash", main_gate["reason"])
        real_merge_calls = [c for c in fake_git.calls if c[0][:1] == ["merge"] and c[1] == "/repos/my-app"]
        self.assertEqual(real_merge_calls, [])

    def test_spaced_backlog_style_overlap_is_caught_despite_the_quoting_mismatch(self):
        # Task-35's exact live bug: git status --porcelain quotes a
        # spaced path (every real Backlog.md task filename) while git
        # diff --name-only emits the SAME path bare -- reproduced here
        # with that exact asymmetry, using a realistic spaced fixture
        # name rather than the unspaced ones the rest of this file uses
        # (the lesson task-35 was filed over: task-34's own tests never
        # would have caught this).
        spaced = "backlog/tasks/task-2 - Fix the login bug.md"
        fake_git = self._all_green_git(
            repo_dirty_porcelain=f'?? "{spaced}"\n',
            repo_diff_paths=[spaced],  # bare, unquoted -- exactly what diff --name-only really does
        )
        with mock.patch.object(server, "list_sessions", return_value=[]), \
             mock.patch.object(server, "run_backlog", return_value=task_view(status="Done")), \
             mock.patch("os.path.isdir", return_value=True), \
             mock.patch.object(server, "run_git", side_effect=fake_git):
            report = harvest.harvest_branch(self.config, "my-app", "TASK-2")

        self.assertFalse(report["merged"], report)
        main_gate = next(g for g in report["gates"] if g["name"] == "mainCheckoutClean")
        self.assertFalse(main_gate["passed"])
        self.assertIn(spaced, main_gate["reason"])
        real_merge_calls = [c for c in fake_git.calls if c[0][:1] == ["merge"] and c[1] == "/repos/my-app"]
        self.assertEqual(real_merge_calls, [])  # never even reached git's own merge attempt

    def test_untracked_file_colliding_with_a_branch_added_path_refuses(self):
        # The task-9 collision case: an untracked file locally at the
        # exact path the branch would create.
        fake_git = self._all_green_git(
            repo_dirty_porcelain="?? branch-new-file.txt\n",
            repo_diff_paths=["shared.txt", "branch-new-file.txt"],
        )
        with mock.patch.object(server, "list_sessions", return_value=[]), \
             mock.patch.object(server, "run_backlog", return_value=task_view(status="Done")), \
             mock.patch("os.path.isdir", return_value=True), \
             mock.patch.object(server, "run_git", side_effect=fake_git):
            report = harvest.harvest_branch(self.config, "my-app", "TASK-2")

        self.assertFalse(report["merged"])
        main_gate = next(g for g in report["gates"] if g["name"] == "mainCheckoutClean")
        self.assertFalse(main_gate["passed"])
        self.assertIn("branch-new-file.txt", main_gate["reason"])

    def test_unrelated_untracked_file_merges_fine(self):
        fake_git = self._all_green_git(
            repo_dirty_porcelain="?? scratch-notes.txt\n",
            repo_diff_paths=["shared.txt"],
        )
        with mock.patch.object(server, "list_sessions", return_value=[]), \
             mock.patch.object(server, "run_backlog", return_value=task_view(status="Done")), \
             mock.patch("os.path.isdir", return_value=True), \
             mock.patch.object(server, "run_git", side_effect=fake_git):
            report = harvest.harvest_branch(self.config, "my-app", "TASK-2")

        self.assertTrue(report["merged"])
        self.assertEqual(report["unrelatedDirtyCount"], 1)

    def test_staged_change_refuses_even_when_the_staged_path_is_unrelated(self):
        # Empirically confirmed against real git (see
        # tests_integration/test_harvest_integration.py): a staged
        # change blocks a --no-ff merge unconditionally, even to a path
        # the incoming branch's diff never mentions.
        fake_git = self._all_green_git(
            repo_dirty_porcelain="M  unrelated.txt\n",
            repo_diff_paths=["shared.txt"],  # unrelated.txt is NOT in the merge diff
        )
        with mock.patch.object(server, "list_sessions", return_value=[]), \
             mock.patch.object(server, "run_backlog", return_value=task_view(status="Done")), \
             mock.patch("os.path.isdir", return_value=True), \
             mock.patch.object(server, "run_git", side_effect=fake_git):
            report = harvest.harvest_branch(self.config, "my-app", "TASK-2")

        self.assertFalse(report["merged"])
        main_gate = next(g for g in report["gates"] if g["name"] == "mainCheckoutClean")
        self.assertFalse(main_gate["passed"])
        self.assertIn("unrelated.txt", main_gate["reason"])

    def test_no_dirt_at_all_never_mentions_unrelated_dirty_count(self):
        report = None
        fake_git = self._all_green_git()
        with mock.patch.object(server, "list_sessions", return_value=[]), \
             mock.patch.object(server, "run_backlog", return_value=task_view(status="Done")), \
             mock.patch("os.path.isdir", return_value=True), \
             mock.patch.object(server, "run_git", side_effect=fake_git):
            report = harvest.harvest_branch(self.config, "my-app", "TASK-2")

        self.assertTrue(report["merged"])
        self.assertNotIn("unrelatedDirtyCount", report)

    def test_failure_to_compute_merge_diff_refuses_conservatively(self):
        # Conservative bias: if Centrale can't determine what the merge
        # would touch, it must refuse rather than assume nothing overlaps.
        fake_git = self._all_green_git(
            repo_dirty_porcelain=" M unrelated.txt\n",
            repo_diff_fails=True,
        )
        with mock.patch.object(server, "list_sessions", return_value=[]), \
             mock.patch.object(server, "run_backlog", return_value=task_view(status="Done")), \
             mock.patch("os.path.isdir", return_value=True), \
             mock.patch.object(server, "run_git", side_effect=fake_git):
            report = harvest.harvest_branch(self.config, "my-app", "TASK-2")

        self.assertFalse(report["merged"])
        main_gate = next(g for g in report["gates"] if g["name"] == "mainCheckoutClean")
        self.assertFalse(main_gate["passed"])
        self.assertIn("failed to determine", main_gate["reason"])

    def test_check_command_failure_never_moves_mains_head(self):
        """The critical merge-ordering guarantee: checkCommand runs on the
        merged tree in a temporary worktree, and main is never touched
        (no `git merge` against the real repo at all) if it fails."""
        config = make_config("/worktrees", [{
            "name": "my-app", "path": "/repos/my-app", "checkCommand": "python3 -m unittest discover tests",
        }])
        fake_git = self._all_green_git()
        failing_check = git_proc([], 1, "", "FAILED (failures=1)")
        with mock.patch.object(server, "list_sessions", return_value=[]), \
             mock.patch.object(server, "run_backlog", return_value=task_view(status="Done")), \
             mock.patch("os.path.isdir", return_value=True), \
             mock.patch.object(server, "run_git", side_effect=fake_git), \
             mock.patch.object(server, "run_check_command", return_value=failing_check) as run_check:
            report = harvest.harvest_branch(config, "my-app", "TASK-2")

        run_check.assert_called_once()

        self.assertFalse(report["merged"])
        check_gate = next(g for g in report["gates"] if g["name"] == "checkCommand")
        self.assertFalse(check_gate["passed"])

        # No merge, worktree-remove, or branch-delete against the real
        # repo -- main's HEAD (and the worktree/branch) is untouched.
        real_repo_calls = [c for c in fake_git.calls if c[1] == "/repos/my-app"]
        self.assertFalse(any(c[0][:1] == ["merge"] and "--abort" not in c[0] for c in real_repo_calls))
        self.assertFalse(any(c[0][:2] == ["worktree", "remove"] and c[0][-1] == self.wt_dir for c in real_repo_calls))
        self.assertFalse(any(c[0][:1] == ["branch"] and "-d" in c[0] for c in real_repo_calls))

    def test_merge_conflict_never_moves_mains_head(self):
        fake_git = self._all_green_git(scratch_merge_conflict=True)
        with mock.patch.object(server, "list_sessions", return_value=[]), \
             mock.patch.object(server, "run_backlog", return_value=task_view(status="Done")), \
             mock.patch("os.path.isdir", return_value=True), \
             mock.patch.object(server, "run_git", side_effect=fake_git):
            report = harvest.harvest_branch(self.config, "my-app", "TASK-2")

        self.assertFalse(report["merged"])
        real_repo_calls = [c for c in fake_git.calls if c[1] == "/repos/my-app"]
        self.assertFalse(any(c[0][:1] == ["merge"] and "--abort" not in c[0] for c in real_repo_calls))

    def test_already_merged_branch_reports_cleanly(self):
        fake_git = self._all_green_git(commits_over_main=0)
        with mock.patch.object(server, "list_sessions", return_value=[]), \
             mock.patch.object(server, "run_backlog", return_value=task_view(status="Done")), \
             mock.patch("os.path.isdir", return_value=True), \
             mock.patch.object(server, "run_git", side_effect=fake_git):
            report = harvest.harvest_branch(self.config, "my-app", "TASK-2")
        self.assertFalse(report["merged"])
        wt_gate = next(g for g in report["gates"] if g["name"] == "worktreeClean")
        self.assertIn("nothing to merge", wt_gate["reason"])

    def test_parked_done_branch_merges_via_click_with_check_command(self):
        spaced = "backlog/tasks/task-2 - Finished somewhere else.md"
        config = make_config("/worktrees", [{
            "name": "my-app",
            "path": "/repos/my-app",
            "checkCommand": "python3 -m unittest discover tests",
        }])
        fake_git = self._all_green_git(repo_diff_paths=[spaced])
        with mock.patch.object(server, "list_sessions", return_value=[]), \
             mock.patch.object(
                 server,
                 "run_backlog",
                 return_value=task_view(status="Done", path=spaced),
             ) as run_backlog, \
             mock.patch.object(server, "run_git", side_effect=fake_git), \
             mock.patch.object(
                 server,
                 "run_check_command",
                 return_value=git_proc([], 0, "OK", ""),
             ) as run_check, \
             mock.patch("os.path.isdir", return_value=False):
            report = harvest.harvest_branch(config, "my-app", "TASK-2")

        self.assertTrue(report["merged"])
        self.assertEqual(report["branchCheckout"], {"kind": "none", "path": None})
        self.assertEqual(report["taskPath"], spaced)
        task_done_gate = next(g for g in report["gates"] if g["name"] == "taskDone")
        self.assertTrue(task_done_gate["passed"])
        wt_gate = next(g for g in report["gates"] if g["name"] == "worktreeClean")
        self.assertTrue(wt_gate["passed"])
        self.assertEqual(
            wt_gate["reason"], "no worktree -- nothing uncommitted to protect"
        )
        run_check.assert_called_once()
        branch_read_cwd = run_backlog.call_args.kwargs["cwd"]
        self.assertIn(branch_read_cwd, fake_git.scratch_dirs)
        self.assertNotEqual(branch_read_cwd, self.wt_dir)
        self.assertFalse(any(
            args[:2] == ["worktree", "remove"] and args[-1] == self.wt_dir
            for args, _cwd in fake_git.calls
        ))
        self.assertIn(
            (["branch", "-d", "task/task-2"], "/repos/my-app"),
            fake_git.calls,
        )

    def test_already_merged_branch_via_click_never_touches_git_beyond_the_check(self):
        # Clicking Merge on a branch that's already gone (merged by this
        # click racing another, or removed by hand): a friendly
        # "alreadyMerged" result, no real merge attempt, no worktree/
        # branch cleanup -- there's nothing left to clean up.
        with mock.patch.object(server, "list_sessions") as list_sessions, \
             mock.patch.object(server, "run_backlog") as run_backlog, \
             mock.patch.object(server, "run_git", return_value=git_proc([], 1, "", "")) as run_git:
            report = harvest.harvest_branch(self.config, "my-app", "TASK-2")

        self.assertFalse(report["merged"])
        self.assertTrue(report.get("alreadyMerged"))
        list_sessions.assert_not_called()
        run_backlog.assert_not_called()
        run_git.assert_called_once()
        self.assertEqual(run_git.call_args.args[0][0], "rev-parse")

    def test_adopt_done_edits_commits_reevaluates_and_merges_when_green(self):
        task_path = "backlog/tasks/task-2 - Do the thing.md"
        state = {"edited": False, "committed": False}
        calls = []
        fake_git = self._all_green_git(repo_diff_paths=[task_path])

        def read_task(args, cwd):
            if cwd == self.wt_dir:
                status = "Done" if state["committed"] else "In Progress"
                return task_view(status=status, path=task_path)
            return task_view(status="Done", path=task_path)

        def edit_task(args, cwd):
            self.assertEqual(cwd, self.wt_dir)
            self.assertEqual(args, ["task", "edit", "TASK-2", "-s", "Done"])
            state["edited"] = True
            return git_proc(args, 0, "", "")

        def run_git(args, cwd=None):
            calls.append((list(args), cwd))
            if args[:1] == ["log"]:
                return git_proc(args, 0, "agent: operator verification still pending\n", "")
            if cwd == self.wt_dir and args[:1] == ["status"] and state["edited"] and not state["committed"]:
                return git_proc(args, 0, f' M "{task_path}"\n', "")
            if cwd == self.wt_dir and args[:1] == ["commit"]:
                state["committed"] = True
                return git_proc(args, 0, "[task/task-2 abc] adopt Done\n", "")
            return fake_git(args, cwd)

        with mock.patch.object(server, "list_sessions", return_value=[]), \
             mock.patch.object(server, "run_backlog", side_effect=read_task), \
             mock.patch.object(server, "run_backlog_raw", side_effect=edit_task) as run_edit, \
             mock.patch("os.path.isdir", return_value=True), \
             mock.patch.object(server, "run_git", side_effect=run_git):
            report = harvest.harvest_branch(
                self.config, "my-app", "TASK-2", adopt_done=True
            )

        self.assertTrue(report["merged"])
        self.assertTrue(report["adoptedDone"])
        run_edit.assert_called_once_with(
            ["task", "edit", "TASK-2", "-s", "Done"], cwd=self.wt_dir
        )
        self.assertIn((["add", "--", task_path], self.wt_dir), calls)
        self.assertIn(
            (["commit", "--only", "-m", "backlog: adopt board Done for TASK-2", "--", task_path], self.wt_dir),
            calls,
        )
        self.assertTrue(any(args[:1] == ["merge"] and cwd == "/repos/my-app" for args, cwd in calls))

    def test_adopt_done_refuses_dirty_agent_worktree_before_editing(self):
        task_path = "backlog/tasks/task-2 - Do the thing.md"

        def read_task(args, cwd):
            return task_view(
                status="In Progress" if cwd == self.wt_dir else "Done",
                path=task_path,
            )

        def run_git(args, cwd=None):
            if args[:1] == ["log"]:
                return git_proc(args, 0, "agent: unfinished\n", "")
            if cwd == self.wt_dir and args[:1] == ["status"]:
                return git_proc(args, 0, " M recoverable.py\n", "")
            return git_proc(args, 0, "", "")

        with mock.patch.object(server, "list_sessions", return_value=[]), \
             mock.patch.object(server, "run_backlog", side_effect=read_task), \
             mock.patch.object(server, "run_backlog_raw") as run_edit, \
             mock.patch("os.path.isdir", return_value=True), \
             mock.patch.object(server, "run_git", side_effect=run_git):
            with self.assertRaises(harvest.HarvestError) as ctx:
                harvest.harvest_branch(
                    self.config, "my-app", "TASK-2", adopt_done=True
                )

        self.assertEqual(ctx.exception.status, 409)
        self.assertIn("recoverable work", str(ctx.exception))
        run_edit.assert_not_called()

    def test_discard_action_restores_only_task_path_then_reevaluates_and_merges(self):
        task_path = "backlog/tasks/task-2 - Do the thing.md"
        state = {"restored": False}
        calls = []
        fake_git = self._all_green_git(repo_diff_paths=[task_path])

        def run_git(args, cwd=None):
            calls.append((list(args), cwd))
            if cwd == "/repos/my-app" and args[:1] == ["status"]:
                output = "" if state["restored"] else f' M "{task_path}"\n'
                return git_proc(args, 0, output, "")
            if cwd == "/repos/my-app" and args[:1] == ["restore"]:
                self.assertEqual(args, ["restore", "--worktree", "--", task_path])
                state["restored"] = True
                return git_proc(args, 0, "", "")
            return fake_git(args, cwd)

        with mock.patch.object(server, "list_sessions", return_value=[]), \
             mock.patch.object(server, "run_backlog", return_value=task_view(status="Done", path=task_path)), \
             mock.patch("os.path.isdir", return_value=True), \
             mock.patch.object(server, "run_git", side_effect=run_git):
            report = harvest.harvest_branch(
                self.config, "my-app", "TASK-2", discard_main_task_edit=True
            )

        self.assertTrue(report["merged"])
        self.assertEqual(report["discardedPaths"], [task_path])
        restore_calls = [(args, cwd) for args, cwd in calls if args[:1] == ["restore"]]
        self.assertEqual(restore_calls, [(["restore", "--worktree", "--", task_path], "/repos/my-app")])
        self.assertFalse(any("--force" in args for args, _cwd in restore_calls))

    def test_discard_action_refuses_when_any_additional_path_blocks(self):
        task_path = "backlog/tasks/task-2 - Do the thing.md"
        other_path = "operator-notes.txt"
        calls = []
        fake_git = self._all_green_git(repo_diff_paths=[task_path])

        def run_git(args, cwd=None):
            calls.append((list(args), cwd))
            if cwd == "/repos/my-app" and args[:1] == ["status"]:
                return git_proc(
                    args,
                    0,
                    f' M "{task_path}"\nM  {other_path}\n',
                    "",
                )
            return fake_git(args, cwd)

        with mock.patch.object(server, "list_sessions", return_value=[]), \
             mock.patch.object(server, "run_backlog", return_value=task_view(status="Done", path=task_path)), \
             mock.patch("os.path.isdir", return_value=True), \
             mock.patch.object(server, "run_git", side_effect=run_git):
            report = harvest.harvest_branch(
                self.config, "my-app", "TASK-2", discard_main_task_edit=True
            )

        self.assertFalse(report["merged"])
        self.assertNotIn("discardableMainTaskEdit", report)
        gate = next(g for g in report["gates"] if g["name"] == "mainCheckoutClean")
        self.assertIn(task_path, gate["reason"])
        self.assertIn(other_path, gate["reason"])
        self.assertFalse(any(args[:1] == ["restore"] for args, _cwd in calls))
        self.assertFalse(any(args[:1] == ["merge"] and cwd == "/repos/my-app" for args, cwd in calls))

    def test_cleanup_failure_after_merge_becomes_warning_not_failure(self):
        fake_git = self._all_green_git(worktree_remove_fails=True, branch_delete_fails=True)
        with mock.patch.object(server, "list_sessions", return_value=[]), \
             mock.patch.object(server, "run_backlog", return_value=task_view(status="Done")), \
             mock.patch("os.path.isdir", return_value=True), \
             mock.patch.object(server, "run_git", side_effect=fake_git):
            report = harvest.harvest_branch(self.config, "my-app", "TASK-2")
        self.assertTrue(report["merged"])
        self.assertIn("warnings", report)
        self.assertEqual(len(report["warnings"]), 2)

    def test_parked_cleanup_uses_safe_delete_and_preserves_branch_on_refusal(self):
        fake_git = self._all_green_git(branch_delete_fails=True)
        with mock.patch.object(server, "list_sessions", return_value=[]), \
             mock.patch.object(server, "run_backlog", return_value=task_view(status="Done")), \
             mock.patch("os.path.isdir", return_value=False), \
             mock.patch.object(server, "run_git", side_effect=fake_git):
            report = harvest.harvest_branch(self.config, "my-app", "TASK-2")

        self.assertTrue(report["merged"])
        self.assertEqual(len(report["warnings"]), 1)
        self.assertIn("branch not fully merged", report["warnings"][0])
        delete_calls = [
            args for args, cwd in fake_git.calls
            if args[:1] == ["branch"] and cwd == "/repos/my-app"
        ]
        self.assertEqual(delete_calls, [["branch", "-d", "task/task-2"]])
        self.assertNotIn("-D", delete_calls[0])

    def test_unexpected_real_merge_failure_raises_harvest_error(self):
        # All gates say clean, but the real merge somehow fails anyway
        # (e.g. a race). This must not silently report success.
        fake_git = self._all_green_git(real_merge_fails=True)
        with mock.patch.object(server, "list_sessions", return_value=[]), \
             mock.patch.object(server, "run_backlog", return_value=task_view(status="Done")), \
             mock.patch("os.path.isdir", return_value=True), \
             mock.patch.object(server, "run_git", side_effect=fake_git):
            with self.assertRaises(harvest.HarvestError) as ctx:
                harvest.harvest_branch(self.config, "my-app", "TASK-2")
        self.assertEqual(ctx.exception.status, 500)
        # Must attempt to abort rather than leave the repo mid-merge.
        abort_calls = [c for c in fake_git.calls if c[0][:1] == ["merge"] and "--abort" in c[0] and c[1] == "/repos/my-app"]
        self.assertEqual(len(abort_calls), 1)

    def test_unknown_project_raises(self):
        with self.assertRaises(harvest.HarvestError) as ctx:
            harvest.harvest_branch(self.config, "nonexistent", "TASK-2")
        self.assertEqual(ctx.exception.status, 404)

    def test_invalid_task_id_raises(self):
        with self.assertRaises(harvest.HarvestError) as ctx:
            harvest.harvest_branch(self.config, "my-app", "not an id; rm -rf")
        self.assertEqual(ctx.exception.status, 400)


class EvaluateAllAndHarvestAllTests(unittest.TestCase):
    def setUp(self):
        self.config = make_config("/worktrees", [{"name": "my-app", "path": "/repos/my-app"}])

    def test_evaluate_all_branches_reports_each(self):
        fake_git = FakeGitHarvest(
            "/repos/my-app", "/worktrees/my-app-task-2",
            task_branches=["task/task-2", "task/task-3"], commits_over_main=1,
        )
        with mock.patch.object(server, "list_sessions", return_value=[]), \
             mock.patch.object(server, "run_backlog", return_value=task_view(status="Done")), \
             mock.patch("os.path.isdir", return_value=True), \
             mock.patch.object(server, "run_git", side_effect=fake_git):
            reports = harvest.evaluate_all_branches(self.config, "my-app")
        self.assertEqual({r["taskId"] for r in reports}, {"TASK-2", "TASK-3"})

    def test_evaluate_all_branches_never_acts(self):
        fake_git = FakeGitHarvest(
            "/repos/my-app", "/worktrees/my-app-task-2",
            task_branches=["task/task-2"], commits_over_main=1,
        )
        with mock.patch.object(server, "list_sessions", return_value=[]), \
             mock.patch.object(server, "run_backlog", return_value=task_view(status="Done")), \
             mock.patch("os.path.isdir", return_value=True), \
             mock.patch.object(server, "run_git", side_effect=fake_git):
            harvest.evaluate_all_branches(self.config, "my-app")
        # No real merge, spawn-worktree removal, or branch deletion --
        # GET-style evaluation must never act. `worktree remove` calls
        # against repo_path are expected (that's how a scratch worktree
        # is *always* cleaned up, run from the repo that owns it), as
        # long as none of them target the real task worktree itself.
        real_repo_calls = [c for c in fake_git.calls if c[1] == "/repos/my-app"]
        self.assertFalse(any(c[0][:1] == ["merge"] and "--abort" not in c[0] for c in real_repo_calls))
        self.assertFalse(any(
            c[0][:2] == ["worktree", "remove"] and c[0][-1] not in fake_git.scratch_dirs
            for c in real_repo_calls
        ))
        self.assertFalse(any(c[0][:1] == ["branch"] and "-d" in c[0] for c in real_repo_calls))

    def test_evaluate_all_branches_unknown_project_raises(self):
        with self.assertRaises(harvest.HarvestError) as ctx:
            harvest.evaluate_all_branches(self.config, "nonexistent")
        self.assertEqual(ctx.exception.status, 404)

    def test_harvest_all_ready_merges_only_the_green_ones(self):
        def fake_run_backlog(args, cwd):
            task_id = args[2]
            status = "Done" if task_id == "TASK-2" else "In Progress"
            return task_view(status=status)

        fake_git = FakeGitHarvest(
            "/repos/my-app", "/worktrees/my-app-task-2",
            task_branches=["task/task-2", "task/task-3"], commits_over_main=1,
        )

        def fake_isdir(path):
            return True  # both worktrees "exist" for this test

        with mock.patch.object(server, "list_sessions", return_value=[]), \
             mock.patch.object(server, "run_backlog", side_effect=fake_run_backlog), \
             mock.patch("os.path.isdir", side_effect=fake_isdir), \
             mock.patch.object(server, "run_git", side_effect=fake_git):
            result = harvest.harvest_all_ready(self.config, "my-app")

        merged_ids = {r["taskId"] for r in result["merged"]}
        not_ready_ids = {r["taskId"] for r in result["notReady"]}
        self.assertEqual(merged_ids, {"TASK-2"})
        self.assertEqual(not_ready_ids, {"TASK-3"})

    def test_harvest_all_ready_lands_an_external_branch_in_not_ready(self):
        # task-81 AC #4: "Merge all ready" and auto mode are untouched by
        # the card/drawer disabled-Merge rule -- harvest_all_ready still
        # evaluates every task/<id> branch server-side, and one checked
        # out outside Centrale fails gate 2 (taskDone) into notReady
        # naming the foreign path.
        foreign = "/repos/my-app/.worktrees/task-2-integrated"
        porcelain = (
            "worktree /repos/my-app\nHEAD aaa\nbranch refs/heads/main\n\n"
            f"worktree {foreign}\nHEAD bbb\nbranch refs/heads/task/task-2\n"
        )

        def fake_git(args, cwd=None):
            if args[:2] == ["worktree", "list"]:
                return git_proc(args, 0, porcelain, "")
            if args[:1] == ["for-each-ref"]:
                return git_proc(args, 0, "task/task-2\n", "")
            if args[:1] == ["log"]:
                return git_proc(args, 0, "1700000000\n", "")
            return git_proc(args, 0, "", "")

        def fake_isdir(path):
            return path == "/repos/my-app"

        for trigger in ("click", "auto"):
            with self.subTest(trigger=trigger), \
                 mock.patch.object(server, "list_sessions", return_value=[]), \
                 mock.patch.object(server, "run_backlog") as run_backlog, \
                 mock.patch("os.path.isdir", side_effect=fake_isdir), \
                 mock.patch.object(server, "run_git", side_effect=fake_git):
                result = harvest.harvest_all_ready(self.config, "my-app", trigger=trigger)
            self.assertEqual(result["merged"], [])
            self.assertEqual([r["taskId"] for r in result["notReady"]], ["TASK-2"])
            report = result["notReady"][0]
            gate = report["gates"][1]
            self.assertEqual(gate["name"], "taskDone")
            self.assertFalse(gate["passed"])
            self.assertIn(foreign, gate["reason"])
            self.assertIn("outside Centrale", gate["reason"])
            self.assertEqual(report["branchCheckout"]["kind"], "external")
            run_backlog.assert_not_called()

    def test_auto_path_merges_parked_done_branch_with_check_command(self):
        harvest._reset_events()
        self.addCleanup(harvest._reset_events)
        config = make_config("/worktrees", [{
            "name": "my-app",
            "path": "/repos/my-app",
            "checkCommand": "python3 -m unittest discover tests",
        }])
        fake_git = FakeGitHarvest(
            "/repos/my-app",
            "/worktrees/my-app-task-2",
            task_branches=["task/task-2"],
            commits_over_main=1,
        )

        def fake_isdir(path):
            # The configured repo exists; the Centrale task worktree does not.
            return path == "/repos/my-app"

        with mock.patch.object(server, "list_sessions", return_value=[]), \
             mock.patch.object(server, "run_backlog", return_value=task_view(status="Done")), \
             mock.patch.object(server, "run_git", side_effect=fake_git), \
             mock.patch.object(
                 server,
                 "run_check_command",
                 return_value=git_proc([], 0, "OK", ""),
             ) as run_check, \
             mock.patch("os.path.isdir", side_effect=fake_isdir):
            result = harvest.harvest_all_ready(config, "my-app", trigger="auto")

        self.assertEqual([r["taskId"] for r in result["merged"]], ["TASK-2"])
        self.assertEqual(result["notReady"], [])
        run_check.assert_called_once()
        events = harvest.recent_events("my-app")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["trigger"], "auto")
        self.assertTrue(events[0]["merged"])
        self.assertIn(
            (["branch", "-d", "task/task-2"], "/repos/my-app"),
            fake_git.calls,
        )


class HarvestEventsTests(unittest.TestCase):
    """Every harvest *attempt* (never an evaluate-only GET) is recorded
    to the in-memory events log, tagged by trigger, so auto-harvest
    outcomes are observable via GET /api/harvest's "events" list."""

    def setUp(self):
        harvest._reset_events()
        self.addCleanup(harvest._reset_events)
        self.config = make_config("/worktrees", [{"name": "my-app", "path": "/repos/my-app"}])
        self.wt_dir = "/worktrees/my-app-task-2"

    def test_successful_click_harvest_records_a_merged_event(self):
        fake_git = FakeGitHarvest("/repos/my-app", self.wt_dir, commits_over_main=1)
        with mock.patch.object(server, "list_sessions", return_value=[]), \
             mock.patch.object(server, "run_backlog", return_value=task_view(status="Done")), \
             mock.patch("os.path.isdir", return_value=True), \
             mock.patch.object(server, "run_git", side_effect=fake_git):
            harvest.harvest_branch(self.config, "my-app", "TASK-2")

        events = harvest.recent_events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["trigger"], "click")
        self.assertTrue(events[0]["merged"])
        self.assertEqual(events[0]["project"], "my-app")
        self.assertEqual(events[0]["taskId"], "TASK-2")

    def test_gate_failure_records_a_blocked_event_with_reason(self):
        with mock.patch.object(server, "list_sessions", return_value=[]), \
             mock.patch.object(server, "run_backlog", return_value=task_view(status="In Progress")), \
             mock.patch.object(server, "run_git", return_value=git_proc([], 0, "", "")), \
             mock.patch("os.path.isdir", return_value=True):
            harvest.harvest_branch(self.config, "my-app", "TASK-2")

        events = harvest.recent_events()
        self.assertEqual(len(events), 1)
        self.assertFalse(events[0]["merged"])
        self.assertIn("taskDone", events[0]["reason"])

    def test_already_merged_attempt_records_a_friendly_event(self):
        with mock.patch.object(server, "list_sessions") as list_sessions, \
             mock.patch.object(server, "run_backlog") as run_backlog, \
             mock.patch.object(server, "run_git", return_value=git_proc([], 1, "", "")):
            harvest.harvest_branch(self.config, "my-app", "TASK-2")

        events = harvest.recent_events()
        self.assertEqual(len(events), 1)
        self.assertFalse(events[0]["merged"])
        self.assertTrue(events[0].get("alreadyMerged"))
        self.assertIn("already merged", events[0]["reason"])
        self.assertNotIn("error", events[0])
        list_sessions.assert_not_called()
        run_backlog.assert_not_called()

    def test_auto_trigger_is_tagged_on_the_event(self):
        with mock.patch.object(server, "list_sessions", return_value=[]), \
             mock.patch.object(server, "run_backlog", return_value=task_view(status="In Progress")), \
             mock.patch.object(server, "run_git", return_value=git_proc([], 0, "", "")), \
             mock.patch("os.path.isdir", return_value=True):
            harvest.harvest_branch(self.config, "my-app", "TASK-2", trigger="auto")

        events = harvest.recent_events()
        self.assertEqual(events[0]["trigger"], "auto")

    def test_unexpected_merge_failure_still_records_an_event(self):
        fake_git = FakeGitHarvest("/repos/my-app", self.wt_dir, commits_over_main=1, real_merge_fails=True)
        with mock.patch.object(server, "list_sessions", return_value=[]), \
             mock.patch.object(server, "run_backlog", return_value=task_view(status="Done")), \
             mock.patch("os.path.isdir", return_value=True), \
             mock.patch.object(server, "run_git", side_effect=fake_git):
            with self.assertRaises(harvest.HarvestError):
                harvest.harvest_branch(self.config, "my-app", "TASK-2")

        events = harvest.recent_events()
        self.assertEqual(len(events), 1)
        self.assertFalse(events[0]["merged"])
        self.assertIn("error", events[0])

    def test_evaluate_only_get_never_records_an_event(self):
        fake_git = FakeGitHarvest(
            "/repos/my-app", self.wt_dir, task_branches=["task/task-2"], commits_over_main=1,
        )
        with mock.patch.object(server, "list_sessions", return_value=[]), \
             mock.patch.object(server, "run_backlog", return_value=task_view(status="Done")), \
             mock.patch("os.path.isdir", return_value=True), \
             mock.patch.object(server, "run_git", side_effect=fake_git):
            harvest.evaluate_all_branches(self.config, "my-app")

        self.assertEqual(harvest.recent_events(), [])

    def test_recent_events_filters_by_project(self):
        config = make_config("/worktrees", [
            {"name": "my-app", "path": "/repos/my-app"},
            {"name": "my-tool", "path": "/repos/my-tool"},
        ])
        with mock.patch.object(server, "list_sessions", return_value=[]), \
             mock.patch.object(server, "run_backlog", return_value=task_view(status="In Progress")), \
             mock.patch.object(server, "run_git", return_value=git_proc([], 0, "", "")), \
             mock.patch("os.path.isdir", return_value=True):
            harvest.harvest_branch(config, "my-app", "TASK-2")
            harvest.harvest_branch(config, "my-tool", "TASK-3")

        my_app_events = harvest.recent_events("my-app")
        self.assertEqual(len(my_app_events), 1)
        self.assertEqual(my_app_events[0]["project"], "my-app")
        self.assertEqual(len(harvest.recent_events()), 2)

    def test_events_log_is_bounded(self):
        with mock.patch.object(server, "list_sessions", return_value=[]), \
             mock.patch.object(server, "run_backlog", return_value=task_view(status="In Progress")), \
             mock.patch.object(server, "run_git", return_value=git_proc([], 0, "", "")), \
             mock.patch("os.path.isdir", return_value=True):
            for i in range(60):
                harvest.harvest_branch(self.config, "my-app", f"TASK-{i + 1}")
        self.assertLessEqual(len(harvest.recent_events()), 50)


class HarvestProgressTests(unittest.TestCase):
    """Task-96: the live record of what the ONE in-flight harvest is
    doing right now, so a click can say "Running tests" instead of
    staring at an unexplained "Merging…" for the length of a test suite.

    Every stage here is OBSERVED from inside a running harvest -- each
    fake subprocess boundary snapshots harvest.current_progress() as it
    is called -- rather than asserted against the known gate order. That
    is the point of the feature: the frontend is told what the server is
    doing, and never infers it.
    """

    def setUp(self):
        self.wt_dir = "/worktrees/my-app-task-2"
        self.config = make_config("/worktrees", [{
            "name": "my-app",
            "path": "/repos/my-app",
            "checkCommand": "python3 -m unittest discover tests",
        }])
        harvest._reset_events()
        harvest._clear_progress()
        self.addCleanup(harvest._reset_events)
        self.addCleanup(harvest._clear_progress)

    # -- observation helpers --------------------------------------------

    def _run_observed(self, config=None, task_branches=None, **git_kwargs):
        """Runs one harvest (or, with task_branches, one evaluate-only
        pass) with every subprocess boundary snapshotting the live
        progress record. Returns (trace, result_or_error)."""
        trace = []
        git_kwargs.setdefault("commits_over_main", 1)
        fake_git = FakeGitHarvest(
            "/repos/my-app", self.wt_dir, task_branches=task_branches or [], **git_kwargs
        )

        def snapshot():
            trace.append(harvest.current_progress())

        def git(args, cwd=None):
            snapshot()
            return fake_git(args, cwd=cwd)

        def sessions():
            snapshot()
            return []

        def backlog(args, cwd=None):
            snapshot()
            return task_view(status="Done", title="Fix the bug")

        def check(command, cwd=None, timeout=None):
            snapshot()
            return git_proc([], 0, "OK", "")

        with mock.patch.object(server, "list_sessions", side_effect=sessions), \
             mock.patch.object(server, "run_backlog", side_effect=backlog), \
             mock.patch.object(server, "run_check_command", side_effect=check), \
             mock.patch("os.path.isdir", return_value=True), \
             mock.patch.object(server, "run_git", side_effect=git):
            active = config if config is not None else self.config
            if task_branches:
                result = harvest.evaluate_all_branches(active, "my-app")
            else:
                result = harvest.harvest_branch(active, "my-app", "TASK-2")
        return trace, result

    @staticmethod
    def _stages(trace):
        """The stage names the trace passed through, consecutive
        duplicates collapsed -- one entry per stage actually entered."""
        stages = []
        for snapshot in trace:
            name = snapshot["gate"] if snapshot else None
            if not stages or stages[-1] != name:
                stages.append(name)
        return stages

    # -- AC #1 / #7: the stages, observed in order --------------------

    def test_every_stage_is_observed_in_order_during_a_merge(self):
        trace, report = self._run_observed()
        self.assertTrue(report["merged"])

        stages = self._stages(trace)
        # The first snapshot is the branch-existence pre-check, before
        # any gate has started: the attempt is already published, with no
        # stage yet, which the frontend renders as its plain fallback.
        self.assertIsNone(stages[0])
        self.assertEqual(stages[1:], harvest.PROGRESS_STAGES)
        # ...which is the five gates, then the pre-merge re-check of the
        # project's own checkout, then the merge itself.
        self.assertEqual(harvest.PROGRESS_STAGES, [
            "noLiveSession", "taskDone", "worktreeClean", "mergeClean",
            "checkCommand", "mainCheckoutClean", "merge",
        ])

    def test_each_snapshot_names_the_branch_and_trigger_it_belongs_to(self):
        trace, _ = self._run_observed()
        for snapshot in trace:
            self.assertEqual(snapshot["project"], "my-app")
            self.assertEqual(snapshot["taskId"], "TASK-2")
            self.assertEqual(snapshot["branch"], "task/task-2")
            self.assertEqual(snapshot["trigger"], "click")
        # AC #2: nothing timing-shaped is published at all -- no start
        # time, elapsed count, estimate or percentage for a UI to show.
        self.assertEqual(
            sorted(trace[0]), ["branch", "gate", "project", "taskId", "trigger"]
        )

    def test_the_check_gate_is_reported_only_while_it_actually_runs(self):
        # A project with no checkCommand passes gate 5 vacuously, with
        # nothing run -- saying "running tests" for that would be a
        # guess, so the stage is never entered (AC #6: such a merge has
        # nothing extra to show, and shows nothing extra).
        config = make_config("/worktrees", [{"name": "my-app", "path": "/repos/my-app"}])
        trace, report = self._run_observed(config=config)
        self.assertTrue(report["merged"])
        self.assertNotIn("checkCommand", self._stages(trace))
        self.assertEqual(self._stages(trace)[1:], [
            "noLiveSession", "taskDone", "worktreeClean", "mergeClean",
            "mainCheckoutClean", "merge",
        ])

    # -- AC #4: nothing survives a terminal outcome -------------------

    def test_nothing_is_readable_when_no_harvest_is_running(self):
        self.assertIsNone(harvest.current_progress())

    def test_the_record_is_cleared_after_a_successful_merge(self):
        _, report = self._run_observed()
        self.assertTrue(report["merged"])
        self.assertIsNone(harvest.current_progress())

    def test_the_record_is_cleared_after_a_gate_failure(self):
        sessions = [{"name": "centrale-my-app-task-2", "created": "0", "attached": False}]
        with mock.patch.object(server, "list_sessions", return_value=sessions), \
             mock.patch.object(server, "run_git", return_value=git_proc([], 0, "", "")):
            report = harvest.harvest_branch(self.config, "my-app", "TASK-2")
        self.assertFalse(report["merged"])
        self.assertIsNone(harvest.current_progress())

    def test_the_record_is_cleared_after_a_harvest_error(self):
        with self.assertRaises(harvest.HarvestError):
            self._run_observed(real_merge_fails=True)
        self.assertIsNone(harvest.current_progress())

    def test_the_record_is_cleared_after_an_unexpected_exception(self):
        # Not a HarvestError and not a gate failure -- a bug. The record
        # must not outlive it either, or the button would sit on a stage
        # name for a harvest that is no longer running.
        with mock.patch.object(
            harvest, "_harvest_branch_locked", side_effect=RuntimeError("boom")
        ), self.assertRaises(RuntimeError):
            harvest.harvest_branch(self.config, "my-app", "TASK-2")
        self.assertIsNone(harvest.current_progress())

    # -- AC #3: a read-only evaluation publishes nothing ---------------

    def test_an_evaluate_only_pass_publishes_no_progress(self):
        # GET /api/harvest walks the same gates but never acts and never
        # takes the merge lock, so it must leave the record alone --
        # otherwise a background board refresh would relabel a button
        # for a merge nobody asked for.
        trace, reports = self._run_observed(task_branches=["task/task-2"])
        self.assertEqual(len(reports), 1)
        self.assertTrue(trace)
        self.assertEqual([s for s in trace if s is not None], [])
        self.assertIsNone(harvest.current_progress())

    # -- AC #7: auto-harvest uses this record, not a second one --------

    def test_auto_harvest_cycles_report_through_the_same_record(self):
        config = make_config("/worktrees", [{
            "name": "my-app",
            "path": "/repos/my-app",
            "checkCommand": "python3 -m unittest discover tests",
        }])
        config["harvest"] = {"mode": "auto"}
        trace = []
        fake_git = FakeGitHarvest(
            "/repos/my-app", self.wt_dir, task_branches=["task/task-2"], commits_over_main=1
        )

        def git(args, cwd=None):
            trace.append(harvest.current_progress())
            return fake_git(args, cwd=cwd)

        with mock.patch.object(server, "list_sessions", return_value=[]), \
             mock.patch.object(server, "run_backlog", return_value=task_view(status="Done")), \
             mock.patch.object(server, "run_check_command", return_value=git_proc([], 0, "", "")), \
             mock.patch("os.path.isdir", return_value=True), \
             mock.patch.object(server, "run_git", side_effect=git):
            harvest.AutoHarvestThread(config, sleep_fn=lambda _s: None).run_cycle()

        published = [s for s in trace if s is not None]
        self.assertTrue(published)
        self.assertEqual({s["trigger"] for s in published}, {"auto"})
        # The same stages a click walks, through the same record -- an
        # auto cycle reaches harvest_branch() by the same one code path.
        self.assertEqual(
            [s for s in self._stages(trace) if s is not None],
            ["worktreeClean", "mergeClean", "checkCommand", "mainCheckoutClean", "merge"],
        )
        self.assertIsNone(harvest.current_progress())

    # -- the reader itself ---------------------------------------------

    def test_current_progress_hands_back_a_copy(self):
        harvest._begin_progress("my-app", "TASK-2", "task/task-2", "click")
        snapshot = harvest.current_progress()
        snapshot["gate"] = "tampered"
        self.assertIsNone(harvest.current_progress()["gate"])
        harvest._set_progress_stage("checkCommand")
        self.assertEqual(harvest.current_progress()["gate"], "checkCommand")

    def test_setting_a_stage_with_nothing_in_flight_is_a_no_op(self):
        harvest._set_progress_stage("checkCommand")
        self.assertIsNone(harvest.current_progress())


class HarvestLockingTests(unittest.TestCase):
    """harvest_branch() serializes with every other harvest attempt
    (click or auto) system-wide via _harvest_lock, so two attempts can
    never interleave their git operations."""

    def setUp(self):
        harvest._reset_events()
        self.addCleanup(harvest._reset_events)
        self.config = make_config("/worktrees", [{"name": "my-app", "path": "/repos/my-app"}])

    def test_lock_is_held_for_the_duration_of_an_attempt(self):
        observed = {}

        def check_locked():
            observed["locked_during_attempt"] = harvest._harvest_lock.locked()
            return []

        with mock.patch.object(server, "list_sessions", side_effect=check_locked), \
             mock.patch.object(server, "run_backlog", return_value=task_view(status="In Progress")), \
             mock.patch.object(server, "run_git", return_value=git_proc([], 0, "", "")):
            harvest.harvest_branch(self.config, "my-app", "TASK-2")

        self.assertTrue(observed["locked_during_attempt"])
        # ...and released again afterward, for the next attempt.
        self.assertFalse(harvest._harvest_lock.locked())

    def test_concurrent_attempts_never_overlap(self):
        """Empirical proof: three harvest attempts, each holding a
        marker open for a short window while "inside", must never
        observe more than one marker open at once."""
        overlap_detected = threading.Event()
        inside_count = {"n": 0}
        count_lock = threading.Lock()

        def tracking_list_sessions():
            with count_lock:
                inside_count["n"] += 1
                if inside_count["n"] > 1:
                    overlap_detected.set()
            time.sleep(0.05)
            with count_lock:
                inside_count["n"] -= 1
            return []

        def worker(task_id):
            harvest.harvest_branch(self.config, "my-app", task_id)

        # mock.patch's save/restore is not thread-safe: entering/exiting the
        # same patch concurrently from multiple threads can leave the
        # patched attribute permanently corrupted for later tests. Apply the
        # patch once, single-threaded, and only let the worker threads read
        # the already-patched attribute concurrently.
        with mock.patch.object(server, "list_sessions", side_effect=tracking_list_sessions), \
             mock.patch.object(server, "run_backlog", return_value=task_view(status="In Progress")), \
             mock.patch.object(server, "run_git", return_value=git_proc([], 0, "", "")):
            threads = [threading.Thread(target=worker, args=(tid,)) for tid in ("TASK-2", "TASK-3", "TASK-4")]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=5)

        self.assertFalse(overlap_detected.is_set())
        self.assertEqual(len(harvest.recent_events()), 3)


class AutoHarvestThreadTests(unittest.TestCase):
    def setUp(self):
        harvest._reset_events()
        self.addCleanup(harvest._reset_events)

    def test_run_cycle_noops_when_mode_is_click(self):
        config = make_config("/worktrees", [{"name": "my-app", "path": "/repos/my-app"}])
        config["harvest"] = {"mode": "click"}
        thread = harvest.AutoHarvestThread(config)
        with mock.patch("harvest.harvest_all_ready") as fake_harvest_all:
            thread.run_cycle()
        fake_harvest_all.assert_not_called()

    def test_run_cycle_noops_when_harvest_key_absent(self):
        config = make_config("/worktrees", [{"name": "my-app", "path": "/repos/my-app"}])
        thread = harvest.AutoHarvestThread(config)
        with mock.patch("harvest.harvest_all_ready") as fake_harvest_all:
            thread.run_cycle()
        fake_harvest_all.assert_not_called()

    def test_run_cycle_harvests_every_project_when_mode_is_auto(self):
        config = make_config("/worktrees", [
            {"name": "my-app", "path": "/repos/my-app"},
            {"name": "my-tool", "path": "/repos/my-tool"},
        ])
        config["harvest"] = {"mode": "auto"}
        thread = harvest.AutoHarvestThread(config)
        with mock.patch("harvest.harvest_all_ready", return_value={"merged": [], "notReady": []}) as fake:
            thread.run_cycle()
        self.assertEqual(fake.call_count, 2)
        fake.assert_any_call(config, "my-app", trigger="auto")
        fake.assert_any_call(config, "my-tool", trigger="auto")

    def test_run_cycle_reads_mode_live_each_call(self):
        """The same config object, mutated between two calls, must
        change run_cycle()'s behavior without recreating the thread --
        this is what lets a future settings change apply without a
        server restart."""
        config = make_config("/worktrees", [{"name": "my-app", "path": "/repos/my-app"}])
        thread = harvest.AutoHarvestThread(config)

        with mock.patch("harvest.harvest_all_ready") as fake:
            thread.run_cycle()
            fake.assert_not_called()

            config["harvest"] = {"mode": "auto"}
            thread.run_cycle()
            fake.assert_called_once()

    def test_run_cycle_skips_a_projects_harvest_error_without_stopping(self):
        config = make_config("/worktrees", [
            {"name": "my-app", "path": "/repos/my-app"},
            {"name": "my-tool", "path": "/repos/my-tool"},
        ])
        config["harvest"] = {"mode": "auto"}
        thread = harvest.AutoHarvestThread(config)

        def fake_harvest_all(cfg, name, trigger):
            if name == "my-app":
                raise harvest.HarvestError("boom", status=502)
            return {"merged": [], "notReady": []}

        with mock.patch("harvest.harvest_all_ready", side_effect=fake_harvest_all) as fake:
            thread.run_cycle()  # must not raise
        self.assertEqual(fake.call_count, 2)

    def test_gate_one_passes_cleanly_when_tmux_is_unavailable(self):
        """Auto-harvest must not be blocked wholesale by a missing tmux
        -- only gate 1 ("no live session") is naturally affected, and it
        should simply evaluate to "no session" (list_sessions() already
        degrades to [] when the tmux binary itself is missing)."""
        proc = subprocess.CompletedProcess(["tmux"], 127, "", "tmux: No such file or directory")
        with mock.patch.object(server, "run_tmux", return_value=proc):
            sessions = server.list_sessions()
        self.assertEqual(sessions, [])

        ok, reason = harvest._gate_no_live_session("my-app", "TASK-2")
        self.assertTrue(ok)
        self.assertIsNone(reason)

    def test_start_auto_harvest_thread_returns_a_running_daemon_thread(self):
        config = make_config("/worktrees", [])
        thread = harvest.start_auto_harvest_thread(config, interval=1000, sleep_fn=lambda seconds: None)
        try:
            self.assertTrue(thread.is_alive())
            self.assertTrue(thread.daemon)
        finally:
            thread.stop()
            thread.join(timeout=2)

    def test_thread_loop_calls_injected_sleep_repeatedly_and_stops_cleanly(self):
        config = make_config("/worktrees", [])
        config["harvest"] = {"mode": "click"}  # cycles are no-ops; only the loop itself is under test
        sleep_calls = {"n": 0}

        def fast_sleep(seconds):
            sleep_calls["n"] += 1

        thread = harvest.AutoHarvestThread(config, interval=0.001, sleep_fn=fast_sleep)
        thread.start()
        deadline = time.time() + 5
        while sleep_calls["n"] < 3 and time.time() < deadline:
            time.sleep(0.01)
        thread.stop()
        thread.join(timeout=2)

        self.assertFalse(thread.is_alive())
        self.assertGreaterEqual(sleep_calls["n"], 3)


class HarvestHttpApiTests(unittest.TestCase):
    """End-to-end tests against a real ThreadingHTTPServer, with
    harvest.py's own functions mocked so no real git/tmux/backlog calls
    ever happen at the HTTP layer -- harvest.py's own logic is already
    covered directly above."""

    @classmethod
    def setUpClass(cls):
        cls.config = make_config("/worktrees", [{"name": "my-app", "path": "/repos/my-app"}])
        cls.httpd = server.CentraleHTTPServer(("127.0.0.1", 0), server.Handler, cls.config)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join(timeout=5)

    def _url(self, path):
        return f"http://127.0.0.1:{self.port}{path}"

    def _get(self, path):
        try:
            with urllib.request.urlopen(self._url(path), timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def _post(self, path, body_bytes):
        req = urllib.request.Request(
            self._url(path), data=body_bytes, method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_get_harvest_missing_project_param_400(self):
        status, body = self._get("/api/harvest")
        self.assertEqual(status, 400)
        self.assertIn("error", body)

    def test_get_harvest_unknown_project_404(self):
        status, body = self._get("/api/harvest?project=nonexistent")
        self.assertEqual(status, 404)
        self.assertIn("error", body)

    def test_get_harvest_success(self):
        with mock.patch("harvest.evaluate_all_branches", return_value=[{"taskId": "TASK-2", "harvestable": True}]):
            status, body = self._get("/api/harvest?project=my-app")
        self.assertEqual(status, 200)
        self.assertEqual(body["branches"], [{"taskId": "TASK-2", "harvestable": True}])
        self.assertIn("events", body)

    def test_get_harvest_includes_events_for_the_requested_project(self):
        with mock.patch("harvest.evaluate_all_branches", return_value=[]), \
             mock.patch("harvest.recent_events", return_value=[{"project": "my-app", "taskId": "TASK-2"}]) as fn:
            status, body = self._get("/api/harvest?project=my-app")
        self.assertEqual(status, 200)
        self.assertEqual(body["events"], [{"project": "my-app", "taskId": "TASK-2"}])
        fn.assert_called_once_with("my-app")

    def test_post_harvest_one_success(self):
        payload = json.dumps({"project": "my-app", "taskId": "TASK-2"}).encode("utf-8")
        with mock.patch("harvest.harvest_branch", return_value={"merged": True, "taskId": "TASK-2"}) as fn:
            status, body = self._post("/api/harvest", payload)
        self.assertEqual(status, 200)
        self.assertTrue(body["merged"])
        fn.assert_called_once_with(self.config, "my-app", "TASK-2")

    def test_post_harvest_forwards_adopt_done_action(self):
        payload = json.dumps({
            "project": "my-app", "taskId": "TASK-2", "adoptDone": True,
        }).encode("utf-8")
        with mock.patch(
            "harvest.harvest_branch", return_value={"merged": True, "adoptedDone": True}
        ) as fn:
            status, body = self._post("/api/harvest", payload)
        self.assertEqual(status, 200)
        self.assertTrue(body["adoptedDone"])
        fn.assert_called_once_with(
            self.config, "my-app", "TASK-2", adopt_done=True
        )

    def test_post_harvest_forwards_discard_action(self):
        payload = json.dumps({
            "project": "my-app",
            "taskId": "TASK-2",
            "discardMainTaskEdit": True,
        }).encode("utf-8")
        with mock.patch(
            "harvest.harvest_branch", return_value={"merged": True, "discardedPaths": ["task.md"]}
        ) as fn:
            status, body = self._post("/api/harvest", payload)
        self.assertEqual(status, 200)
        self.assertEqual(body["discardedPaths"], ["task.md"])
        fn.assert_called_once_with(
            self.config, "my-app", "TASK-2", discard_main_task_edit=True
        )

    def test_post_harvest_rejects_non_boolean_action_flag(self):
        payload = json.dumps({
            "project": "my-app", "taskId": "TASK-2", "adoptDone": "yes",
        }).encode("utf-8")
        with mock.patch("harvest.harvest_branch") as fn:
            status, body = self._post("/api/harvest", payload)
        self.assertEqual(status, 400)
        self.assertIn("booleans", body["error"])
        fn.assert_not_called()

    def test_post_harvest_already_merged_is_200_not_an_error(self):
        # The friendly "already merged" result travels through the HTTP
        # layer as an ordinary 200, same as any other harvest_branch()
        # result -- it's a normal outcome, not a HarvestError.
        payload = json.dumps({"project": "my-app", "taskId": "TASK-2"}).encode("utf-8")
        with mock.patch("harvest.harvest_branch", return_value={
            "merged": False, "alreadyMerged": True, "taskId": "TASK-2", "branch": "task/task-2", "gates": [],
        }):
            status, body = self._post("/api/harvest", payload)
        self.assertEqual(status, 200)
        self.assertFalse(body["merged"])
        self.assertTrue(body["alreadyMerged"])

    def test_post_harvest_all_ready(self):
        payload = json.dumps({"project": "my-app", "all": True}).encode("utf-8")
        with mock.patch("harvest.harvest_all_ready", return_value={"merged": [], "notReady": []}) as fn:
            status, body = self._post("/api/harvest", payload)
        self.assertEqual(status, 200)
        self.assertEqual(body, {"merged": [], "notReady": []})
        fn.assert_called_once_with(self.config, "my-app")

    def test_post_harvest_surfaces_harvest_error_status(self):
        payload = json.dumps({"project": "my-app", "taskId": "TASK-2"}).encode("utf-8")
        with mock.patch("harvest.harvest_branch", side_effect=harvest.HarvestError("nope", status=409)):
            status, body = self._post("/api/harvest", payload)
        self.assertEqual(status, 409)
        self.assertIn("error", body)

    def test_post_harvest_malformed_json_returns_400(self):
        status, body = self._post("/api/harvest", b"{not valid")
        self.assertEqual(status, 400)
        self.assertIn("error", body)

    # -- GET /api/harvest-progress (task-96) ---------------------------

    def test_get_harvest_progress_is_null_when_nothing_is_harvesting(self):
        harvest._clear_progress()
        status, body = self._get("/api/harvest-progress")
        self.assertEqual(status, 200)
        self.assertIsNone(body["progress"])

    def test_get_harvest_progress_returns_the_live_record_verbatim(self):
        self.addCleanup(harvest._clear_progress)
        harvest._begin_progress("my-app", "TASK-2", "task/task-2", "click")
        harvest._set_progress_stage("checkCommand")
        status, body = self._get("/api/harvest-progress")
        self.assertEqual(status, 200)
        self.assertEqual(body["progress"], {
            "project": "my-app", "taskId": "TASK-2", "branch": "task/task-2",
            "trigger": "click", "gate": "checkCommand",
        })

    def test_progress_is_readable_while_a_merge_post_is_still_outstanding(self):
        """The whole point of the endpoint: the merge POST blocks for as
        long as the gates take -- seconds of test suite on a project with
        a checkCommand -- and the poll has to be answered DURING it.
        Proven here against the real threading server rather than
        assumed from ThreadingHTTPServer's docs."""
        self.addCleanup(harvest._clear_progress)
        started = threading.Event()
        release = threading.Event()

        def slow_harvest(config, project_name, task_id, **kwargs):
            harvest._begin_progress(project_name, task_id, "task/task-2", "click")
            harvest._set_progress_stage("checkCommand")
            started.set()
            release.wait(10)
            harvest._clear_progress()
            return {"merged": True, "taskId": task_id, "branch": "task/task-2", "gates": []}

        posted = {}

        def post():
            payload = json.dumps({"project": "my-app", "taskId": "TASK-2"}).encode("utf-8")
            posted["result"] = self._post("/api/harvest", payload)

        with mock.patch("harvest.harvest_branch", side_effect=slow_harvest):
            worker = threading.Thread(target=post, daemon=True)
            worker.start()
            self.assertTrue(started.wait(10), "the merge POST never started")
            status, body = self._get("/api/harvest-progress")
            release.set()
            worker.join(timeout=10)

        self.assertEqual(status, 200)
        self.assertEqual(body["progress"]["gate"], "checkCommand")
        self.assertEqual(body["progress"]["taskId"], "TASK-2")
        self.assertTrue(posted["result"][1]["merged"])
        # ...and the moment the merge is done there is nothing left to read.
        self.assertIsNone(self._get("/api/harvest-progress")[1]["progress"])


class HarvestDivergenceFrontendTests(unittest.TestCase):
    """Structural coverage for the vanilla, build-free drawer controls."""

    @classmethod
    def setUpClass(cls):
        # task-89: the frontend JS these slices grep is split per concern
        # under static/ -- the merge controls in harvest.js, the Resume
        # POST in spawn.js and the drawer's harvest area in drawer.js.
        static_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static")

        def read(name):
            with open(os.path.join(static_dir, name), "r", encoding="utf-8") as f:
                return f.read()

        cls.js = read("harvest.js")
        cls.spawn = read("spawn.js")
        cls.drawer = read("drawer.js")

    def _function(self, name, next_name, js=None):
        js = self.js if js is None else js
        start = js.index(f"function {name}(")
        end = js.index(f"function {next_name}(", start)
        return js[start:end]

    @staticmethod
    def _last_function(js, name):
        """A function that ends its file (renderDrawerHarvestArea in
        drawer.js, resumeTask in spawn.js): sliced by its own closing
        brace rather than by whatever follows it."""
        start = js.index(f"function {name}(")
        return js[start:js.index("\n  }\n", start) + 5]

    def _drawer_harvest_area(self):
        return self._last_function(self.drawer, "renderDrawerHarvestArea")

    def test_divergence_status_names_agent_branch_and_board_main_checkout(self):
        body = self._function("renderHarvestStatusLine", "harvestAllReady")
        self.assertIn("doneDivergence", body)
        self.assertIn("agent branch status is", body)
        self.assertIn("board/main checkout", body)
        self.assertIn("branchStatus", body)
        self.assertIn("boardStatus", body)
        # The old generic gate reason remains the fallback when there is
        # no divergence metadata.
        self.assertIn('failed ? ("Not ready: " + failed.reason)', body)

    def test_adopt_confirm_surfaces_last_commit_and_posts_explicit_action(self):
        display = self._function("adoptDoneButtonDisplay", "handleDiscardMainClick")
        request = self._function("harvestTask", "harvestButtonDisplay")
        drawer = self._drawer_harvest_area()
        self.assertIn("lastBranchCommitSubject", display)
        self.assertIn("Last branch commit:", display)
        self.assertIn("Confirm adopt Done & merge?", display)
        self.assertIn("payload.adoptDone = true", request)
        self.assertIn("C.handleAdoptDoneClick", drawer)

    def test_reconcile_offer_is_gated_on_behind_base_and_posts_resume_flag(self):
        # task-66: the hint text rides on the blocked status line, the
        # offer sits under it keyed on report.behindBase only, and the
        # confirm posts the ordinary /api/resume with reconcile:true.
        status_line = self._function("renderHarvestStatusLine", "harvestAllReady")
        display = self._function("reconcileButtonDisplay", "renderHarvestStatusLine")
        request = self._last_function(self.spawn, "resumeTask")
        drawer = self._drawer_harvest_area()
        self.assertIn("reconcileHint", status_line)
        self.assertIn("Confirm resume to reconcile?", display)
        self.assertIn("Centrale itself merges nothing", display)
        self.assertIn("payload.reconcile = true", request)
        self.assertIn("report.behindBase", drawer)
        # task-125: the offer is a member of the action area's one
        # secondary row now (the blocked reason it answers is still
        # immediately above it), so the gate is read in the harvest area
        # and the button is built in the row.
        self.assertIn("C.handleReconcileClick",
                      self._last_function(self.drawer, "renderDrawerSecondaryRow"))
        # Its own pending map: arming reconcile can never arm adopt/discard.
        handler = self._function("handleReconcileClick", "reconcileButtonDisplay")
        self.assertIn("C.reconcileConfirmPending", handler)
        self.assertNotIn("adoptDoneConfirmPending", handler)
        self.assertIn("C.resumeTask(projectName, taskId, true)", handler)

    def test_discard_confirm_names_exact_path_and_posts_separate_action(self):
        display = self._function("discardMainButtonDisplay", "renderHarvestStatusLine")
        request = self._function("harvestTask", "harvestButtonDisplay")
        drawer = self._drawer_harvest_area()
        self.assertIn("discardable.path", display)
        self.assertIn("No other path is touched", display)
        self.assertIn("Confirm discard & merge?", display)
        self.assertIn("payload.discardMainTaskEdit = true", request)
        self.assertIn("discardableMainTaskEdit", drawer)


# Number words as docs/merging.md spells its two gate/stage counts, so a
# count that changes fails as a count rather than as a missing substring.
NUMBER_WORDS = [
    "zero", "one", "two", "three", "four", "five", "six", "seven", "eight",
    "nine", "ten",
]

MERGING_DOC = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "docs", "merging.md"
)


class MergingDocGateListTests(unittest.TestCase):
    """docs/merging.md's numbered gate list is pinned to GATE_NAMES.

    Task-144. That list is the chapter a user opens when a merge refuses,
    so a gate named wrong, missing, or in the wrong order there is
    expensive -- and nothing but a human reading both files ever compares
    it to harvest.py. Task-131 set the precedent for how this repo keeps a
    hand-maintained list honest (docs/architecture.md's module table): pin
    the list itself with a small assertion, and check nothing else. So
    only the numbered headings are read here -- the prose under each gate,
    and the document's wording generally, are deliberately not this test's
    business.
    """

    def setUp(self):
        with open(MERGING_DOC, "r", encoding="utf-8") as f:
            self.text = f.read()
        # A sentence the document wraps across lines is still that
        # sentence; and asserting against the flattened copy keeps a
        # failure message from carrying the whole chapter.
        self.flat = re.sub(r"\s+", " ", self.text)

    def assertDocSays(self, phrase, message):
        self.assertTrue(phrase in self.flat, "%s (looked for %r in docs/merging.md)" % (message, phrase))

    def _numbered_gate_headings(self):
        """The bolded heading of each item in the numbered gate list.

        The document's other numbered list (the card/drawer states) opens
        its items with plain text, so requiring the bold immediately after
        "N. " selects the gate list and only the gate list.
        """
        return re.findall(r"^(\d+)\. \*\*(.+?)\*\*", self.text, re.M)

    def test_the_numbered_gate_list_names_every_gate_in_order(self):
        headings = self._numbered_gate_headings()
        self.assertEqual(
            [n for n, _ in headings],
            [str(i + 1) for i in range(len(harvest.GATE_NAMES))],
            "docs/merging.md's numbered gate list does not have one item per "
            "harvest.GATE_NAMES entry: %r" % (headings,))
        # The document spells the gates for a reader ("No live session");
        # harvest.py and the API spell them for a machine ("noLiveSession").
        # Case and spacing are the whole difference, and are all this
        # ignores -- a renamed or reordered gate still fails.
        self.assertEqual(
            [h.replace(" ", "").lower() for _, h in headings],
            [name.lower() for name in harvest.GATE_NAMES],
            "docs/merging.md's numbered gate list has drifted from "
            "harvest.GATE_NAMES (%r) -- fix the document, in order (task-144)"
            % (harvest.GATE_NAMES,))

    def test_the_sixth_gate_is_named_and_counted(self):
        # The one gate that isn't in that list, because it only ever runs
        # on POST: the document has to name it, and has to agree with
        # harvest.py about how many gates that makes.
        self.assertEqual(
            harvest.PROGRESS_STAGES,
            harvest.GATE_NAMES + [harvest.STAGE_MAIN_CHECKOUT_CLEAN, harvest.STAGE_MERGE])
        self.assertDocSays(
            "`%s`" % harvest.STAGE_MAIN_CHECKOUT_CLEAN,
            "docs/merging.md no longer names the POST-only sixth gate")
        gate_count = len(harvest.GATE_NAMES) + 1
        self.assertDocSays(
            "%s explicit safety gates" % NUMBER_WORDS[gate_count],
            "docs/merging.md no longer says there are %d safety gates" % gate_count)

    def test_the_merge_progress_stage_count_matches(self):
        # The button names one stage at a time (task-96); the document
        # says how many others there are to get through, and got that
        # wrong by one from the day it was written.
        others = len(harvest.PROGRESS_STAGES) - 1
        self.assertDocSays(
            "other %s stages are milliseconds each" % NUMBER_WORDS[others],
            "docs/merging.md's merge-progress paragraph no longer says there are "
            "%d stages besides checkCommand" % others)


if __name__ == "__main__":
    unittest.main()
