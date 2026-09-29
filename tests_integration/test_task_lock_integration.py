"""Integration coverage for task-172: while a task is spawned, its task
file has one writer. Spawn locks the main checkout's copy (chmod a-w)
after the claim commit; the lock holds through end-session and resume,
a merge releases it, discard/abandon/cleanup-branch remove it, a stale
one is released at startup and by --check, and POST /api/rule writes a
ruling into the worktree copy when no agent is live.

What only real processes can show, and why this file exists:

  * that the real `backlog` CLI refuses to write a read-only task file
    (EACCES) and leaves it byte-for-byte untouched, and that git status
    sees nothing -- git tracks the executable bit, never the write bit;
  * that a real `git merge` replaces the locked file rather than writing
    into it, and the replacement comes out writable -- the merge
    releasing the lock by itself.

Real git, the real `backlog` CLI, tmux through the per-run test socket
shim (never your tmux server); every path under a private tmp dir. The
HTTP routes run on an in-process server bound to an ephemeral port. No
agent is ever spawned.

Run explicitly: python3 -m unittest tests_integration.test_task_lock_integration
"""

from __future__ import annotations

import json
import os
import stat
import sys
import threading
import unittest
import urllib.error
import urllib.request
from unittest import mock

from tests_integration import base

sys.path.insert(0, base.CENTRALE_ROOT)
import harvest  # noqa: E402
import server  # noqa: E402
import spawn  # noqa: E402


def tearDownModule():
    base.assert_test_tmux_footprint_gone()


def _writable(path):
    return bool(os.stat(path).st_mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))


@base.require_tools("git", "tmux", "backlog")
class TaskFileLockIntegrationTests(base.IntegrationCase):
    PROJECT = "lockproj"

    def setUp(self):
        super().setUp()
        self.repo_path = base.init_backlog_repo(self.tmp_dir, name=self.PROJECT)
        self.worktree_root = os.path.join(self.tmp_dir, "worktrees")
        self.project = {"name": self.PROJECT, "path": self.repo_path, "checkCommand": None}
        self.config = {
            "port": 0,
            "worktreeRoot": self.worktree_root,
            "projects": [self.project],
            "capabilities": {"tmux": True},
        }
        # The delivery log is state Centrale owns outside the repo; keep
        # it in the test's tmp dir.
        log_patch = mock.patch.dict(
            os.environ, {server.DELIVERY_LOG_ENV: os.path.join(self.tmp_dir, "deliveries.jsonl")})
        log_patch.start()
        self.addCleanup(log_patch.stop)

    # -- helpers ------------------------------------------------------------

    def _git(self, *args, cwd=None, check=True):
        return base.run(["git", *args], cwd=cwd or self.repo_path, check=check)

    def _session_alive(self, name):
        proc = base.tmux("list-sessions", "-F", "#{session_name}", check=False)
        if proc.returncode != 0:
            return False
        return name in {line.strip() for line in proc.stdout.splitlines() if line.strip()}

    def _spawn(self, title="Fix the thing"):
        task_id = base.create_task(self.repo_path, title, acceptance_criteria=["do the thing"])
        with mock.patch.dict(os.environ, {"CENTRALE_SPAWN_CMD": base.probe_spawn_cmd(marker="itest-lock")}):
            result = spawn.spawn(self.config, self.PROJECT, task_id)
        name = self.track_session(result["session"])
        base.wait_until(lambda: self._session_alive(name), timeout=10.0,
                        message="spawned session never appeared")
        return task_id, result

    def _end_session(self, task_id):
        name = spawn.session_name(self.PROJECT, task_id)
        base.tmux("kill-session", "-t", name, check=True)
        base.wait_until(lambda: not self._session_alive(name), timeout=10.0,
                        message="session never died")

    def _main_task_file(self, task_id):
        path = spawn.task_file_path(self.repo_path, task_id)
        self.assertIsNotNone(path, f"no task file for {task_id} in the main checkout")
        return path

    def _finish_in_worktree(self, task_id):
        wt_dir = spawn.worktree_dir(self.config, self.PROJECT, task_id)
        with open(os.path.join(wt_dir, "work.txt"), "w", encoding="utf-8") as f:
            f.write("done\n")
        base.run(["backlog", "task", "edit", task_id, "-s", "Done", "--check-ac", "1", "--plain"], cwd=wt_dir)
        base.run(["git", "add", "-A"], cwd=wt_dir)
        base.run(["git", "commit", "-q", "-m", f"finish {task_id}"], cwd=wt_dir)
        return wt_dir

    def _serve(self):
        httpd = server.CentraleHTTPServer(("127.0.0.1", 0), server.Handler, self.config)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        return httpd.server_address[1]

    def _request(self, port, method, path, body=None):
        data = None if body is None else json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}{path}", data=data, method=method,
            headers={"Content-Type": "application/json"} if data is not None else {},
        )
        try:
            with urllib.request.urlopen(req, timeout=30.0) as resp:
                return resp.status, json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read() or b"{}")

    def _reviewed(self, port, task_id):
        status, preview = self._request(
            port, "GET", f"/api/discard-preview?project={self.PROJECT}&task={task_id}")
        self.assertEqual(status, 200, preview)
        return {"project": self.PROJECT, "taskId": task_id,
                "expectedBranchTip": preview["branchTip"],
                "expectedDirtyPaths": preview["dirtyPaths"]}

    # -- AC #1: the lock, and what the CLI and git make of it ---------------

    def test_spawn_locks_main_copy_and_backlog_edit_is_refused_without_a_trace(self):
        task_id, result = self._spawn()
        self.assertNotIn("warnings", result)
        path = self._main_task_file(task_id)
        self.assertIn(" - ", os.path.basename(path))  # a real, spaced Backlog.md filename
        self.assertFalse(_writable(path), "main's task file is still writable after spawn")

        with open(path, "rb") as f:
            before = f.read()
        for args in (["--notes", "a mid-flight note"], ["-s", "Done"], ["-t", "A renamed title"],
                     ["--comment", "a ruling", "--comment-author", "orchestrator"]):
            proc = base.run(["backlog", "task", "edit", task_id, *args, "--plain"],
                            cwd=self.repo_path, check=False)
            self.assertNotEqual(proc.returncode, 0, f"backlog task edit {args} was not refused")
            self.assertIn("EACCES", proc.stdout + proc.stderr)
        with open(self._main_task_file(task_id), "rb") as f:
            self.assertEqual(f.read(), before)
        self.assertEqual(path, self._main_task_file(task_id))  # not renamed
        self.assertEqual(self._git("status", "--porcelain").stdout, "")

        # The board reads the lock back from the file.
        server._reset_board_cache()
        board = server._load_project_board(self.config, self.project)
        flags = {t["id"]: t["taskFileLocked"] for t in board["tasks"]}
        self.assertIs(flags[task_id], True)

    def test_lock_off_spawns_exactly_as_before(self):
        self.config["lockSpawnedTaskFiles"] = False
        task_id, result = self._spawn()
        self.assertNotIn("warnings", result)
        self.assertTrue(_writable(self._main_task_file(task_id)))

    # -- AC #2: end-session, resume, merge ----------------------------------

    def test_lock_survives_end_session_and_resume_and_a_merge_releases_it(self):
        task_id, _ = self._spawn()
        path = self._main_task_file(task_id)

        self._end_session(task_id)
        self.assertFalse(_writable(path), "end-session released the lock")

        resumed = spawn.resume(self.config, self.PROJECT, task_id)
        self.track_session(resumed["session"])
        self.assertFalse(_writable(self._main_task_file(task_id)), "resume released the lock")
        self._end_session(task_id)

        self._finish_in_worktree(task_id)
        report = harvest.harvest_branch(self.config, self.PROJECT, task_id)
        self.assertTrue(report["merged"], report)
        path = self._main_task_file(task_id)
        self.assertTrue(_writable(path), "the merge did not release the lock")
        proc = base.run(["backlog", "task", "edit", task_id, "--notes", "after merge", "--plain"],
                        cwd=self.repo_path, check=False)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_a_plain_git_merge_replaces_the_locked_file_with_a_writable_one(self):
        # The property the design leans on, shown with git alone: no
        # Centrale code between the merge and the assertion.
        task_id, _ = self._spawn()
        self._end_session(task_id)
        self._finish_in_worktree(task_id)
        path = self._main_task_file(task_id)
        self.assertFalse(_writable(path))

        self._git("merge", "--no-ff", "--no-edit", spawn.branch_name(task_id))
        self.assertTrue(_writable(self._main_task_file(task_id)))
        self.assertEqual(self._git("status", "--porcelain").stdout, "")

    # -- AC #2: discard, abandon, cleanup-branch ----------------------------

    def test_discard_unlocks(self):
        task_id, _ = self._spawn()
        self._end_session(task_id)
        port = self._serve()
        status, body = self._request(port, "POST", "/api/discard-attempt", self._reviewed(port, task_id))
        self.assertEqual(status, 200, body)
        path = self._main_task_file(task_id)
        self.assertEqual(body["taskFileUnlocked"], os.path.relpath(path, self.repo_path))
        self.assertTrue(_writable(path))

    def test_abandon_unlocks(self):
        task_id, _ = self._spawn()
        self._end_session(task_id)
        port = self._serve()
        status, body = self._request(port, "POST", "/api/abandon-worktree", self._reviewed(port, task_id))
        self.assertEqual(status, 200, body)
        self.assertTrue(body["branchKept"])
        path = self._main_task_file(task_id)
        self.assertEqual(body["taskFileUnlocked"], os.path.relpath(path, self.repo_path))
        self.assertTrue(_writable(path))

    def test_cleanup_branch_unlocks(self):
        # The out-of-band merge cleanup-branch exists for: the branch is
        # merged by hand, and main's copy is set Done by hand. Unlock is
        # needed for that hand edit first -- exactly the tool-level
        # refusal the lock is for -- so this merges with git (which
        # releases), then re-locks to stand for a merge that did not
        # touch the file.
        task_id, _ = self._spawn()
        self._end_session(task_id)
        self._finish_in_worktree(task_id)
        self._git("merge", "--no-ff", "--no-edit", spawn.branch_name(task_id))
        self.assertIsNone(spawn.lock_task_file(self.repo_path, task_id))
        self.assertFalse(_writable(self._main_task_file(task_id)))

        port = self._serve()
        status, body = self._request(port, "POST", "/api/cleanup-branch",
                                     {"project": self.PROJECT, "taskId": task_id})
        self.assertEqual(status, 200, body)
        path = self._main_task_file(task_id)
        self.assertEqual(body["taskFileUnlocked"], os.path.relpath(path, self.repo_path))
        self.assertTrue(_writable(path))

    # -- AC #3: a stale lock -------------------------------------------------

    def test_a_lock_with_no_branch_behind_it_is_released_at_startup_and_by_check(self):
        task_id, _ = self._spawn()
        self._end_session(task_id)
        wt_dir = spawn.worktree_dir(self.config, self.PROJECT, task_id)
        # Deleted outside Centrale: nothing released the lock.
        self._git("worktree", "remove", "--force", wt_dir)
        self._git("branch", "-D", spawn.branch_name(task_id))
        path = self._main_task_file(task_id)
        self.assertFalse(_writable(path))

        # A second task still spawned keeps its lock through the sweep.
        other_id, _ = self._spawn(title="Still in flight")
        other_path = self._main_task_file(other_id)

        found = spawn.release_stale_task_locks(self.config)
        self.assertEqual([(e["taskId"], e["released"]) for e in found], [(task_id, True)])
        self.assertTrue(_writable(path))
        self.assertFalse(_writable(other_path))

        # --check does the same, and says so.
        os.chmod(path, os.stat(path).st_mode & ~0o222)
        config_path = os.path.join(self.tmp_dir, "projects.json")
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump({"port": base.free_port(), "worktreeRoot": self.worktree_root,
                       "projects": [{"name": self.PROJECT, "path": self.repo_path}]}, f)
        lines, _ok = server.run_doctor_check(config_path)
        stale = [line for line in lines if "stale task-file lock" in line]
        self.assertEqual(len(stale), 1, lines)
        self.assertIn(os.path.relpath(path, self.repo_path), stale[0])
        self.assertTrue(stale[0].startswith("[WARN]"))
        self.assertTrue(_writable(path))
        self.assertFalse(_writable(other_path))

    # -- AC #6: a ruling with no live agent -----------------------------------

    def test_rule_with_no_live_agent_commits_the_comment_on_the_branch_scoped_to_the_file(self):
        task_id, _ = self._spawn()
        self._end_session(task_id)
        wt_dir = spawn.worktree_dir(self.config, self.PROJECT, task_id)
        # Unrelated staged work in the worktree must not ride along.
        with open(os.path.join(wt_dir, "unrelated.txt"), "w", encoding="utf-8") as f:
            f.write("staged, not a ruling\n")
        base.run(["git", "add", "unrelated.txt"], cwd=wt_dir)

        port = self._serve()
        status, body = self._request(port, "POST", "/api/rule", {
            "project": self.PROJECT, "taskId": task_id, "sender": "orchestrator",
            "text": "Keep the old flag; deprecate it next release",
        })
        self.assertEqual(status, 200, body)
        self.assertEqual(body["mode"], "committed")
        rel = os.path.relpath(spawn.task_file_path(wt_dir, task_id), wt_dir)
        self.assertEqual(body["path"], rel)

        branch = spawn.branch_name(task_id)
        tip = self._git("rev-parse", branch).stdout.strip()
        self.assertEqual(body["commit"], tip)
        changed = self._git("diff-tree", "--no-commit-id", "--name-only", "-r", "-z", tip).stdout
        self.assertEqual([p for p in changed.split("\0") if p], [rel])
        self.assertEqual(self._git("log", "-1", "--format=%s", tip).stdout.strip(),
                         f"backlog: ruling on {task_id} from orchestrator")
        # Still staged, still uncommitted.
        self.assertIn("A  unrelated.txt", base.run(["git", "status", "--porcelain"], cwd=wt_dir).stdout)

        view = json.loads(base.run(["backlog", "task", "view", task_id, "--json"], cwd=wt_dir).stdout)
        comments = view["task"]["comments"]
        self.assertEqual(len(comments), 1, comments)
        self.assertIn("Keep the old flag", json.dumps(comments[0]))
        self.assertIn("orchestrator", json.dumps(comments[0]))

        # The main checkout's copy is untouched and still locked.
        self.assertFalse(_writable(self._main_task_file(task_id)))
        self.assertEqual(self._git("status", "--porcelain").stdout, "")

    def test_rule_on_an_unspawned_task_is_refused_and_touches_nothing(self):
        task_id = base.create_task(self.repo_path, "Never spawned")
        port = self._serve()
        status, body = self._request(port, "POST", "/api/rule", {
            "project": self.PROJECT, "taskId": task_id, "sender": "orchestrator", "text": "x",
        })
        self.assertEqual(status, 409, body)
        self.assertIn("not spawned", body["error"])


if __name__ == "__main__":
    unittest.main()
