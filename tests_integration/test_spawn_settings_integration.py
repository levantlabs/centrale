"""Real-process coverage for task-177's per-project spawn settings.

The agent command is a harmless sleep probe. Git, Backlog.md, tmux and the
HTTP routes are real; IntegrationCase isolates their cache, socket and port.

Run explicitly: python3 -m unittest tests_integration.test_spawn_settings_integration
"""

from __future__ import annotations

import json
import os
import threading
import urllib.error
import urllib.request
from unittest import mock

from tests_integration import base

import server
import spawn


def tearDownModule():
    base.assert_test_tmux_footprint_gone()


@base.require_tools("git", "tmux", "backlog")
class SpawnSettingsIntegrationTests(base.IntegrationCase):
    PROJECT = "settingsspawn"

    def setUp(self):
        super().setUp()
        self.repo_path = base.init_backlog_repo(self.tmp_dir, name=self.PROJECT)
        self.venv = os.path.join(self.repo_path, ".venv")
        os.makedirs(os.path.join(self.venv, "bin"))
        with open(os.path.join(self.venv, "bin", "python"), "w", encoding="utf-8") as f:
            f.write("main interpreter sentinel\n")
        self.env_file = os.path.join(self.repo_path, ".env.local")
        with open(self.env_file, "w", encoding="utf-8") as f:
            f.write("MAIN_ONLY=1\n")
        self.project = {
            "name": self.PROJECT,
            "path": self.repo_path,
            "checkCommand": None,
            "worktreeLinks": [".venv", ".env.local"],
        }
        self.config = {
            "port": 0,
            "worktreeRoot": os.path.join(self.tmp_dir, "worktrees"),
            "projects": [self.project],
            "capabilities": {"tmux": True},
        }
        patcher = mock.patch.dict(os.environ, {
            "CENTRALE_SPAWN_CMD": base.probe_spawn_cmd(marker="itest-spawn-settings"),
            server.DELIVERY_LOG_ENV: os.path.join(self.tmp_dir, "deliveries.jsonl"),
        })
        patcher.start()
        self.addCleanup(patcher.stop)

        httpd = server.CentraleHTTPServer(("127.0.0.1", 0), server.Handler, self.config)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        self.port = httpd.server_address[1]

    def _request(self, method, path, body=None):
        data = None if body is None else json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=data, method=method,
            headers={"Content-Type": "application/json"} if data is not None else {},
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as response:
                return response.status, json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read() or b"{}")

    def _git(self, *args, cwd=None, check=True):
        return base.run(["git", *args], cwd=cwd or self.repo_path, check=check)

    def _create_task(self, title):
        task_id = base.create_task(self.repo_path, title, acceptance_criteria=["done"])
        self._git("add", "backlog")
        self._git("commit", "-q", "-m", f"create {task_id}")
        return task_id

    def _spawn(self, task_id):
        status, body = self._request("POST", "/api/spawn", {
            "project": self.PROJECT, "taskId": task_id,
        })
        self.assertEqual(status, 200, body)
        self.track_session(body["session"])
        base.wait_until(
            lambda: body["session"] in self._session_names(),
            message="spawned probe session did not appear",
        )
        return body

    def _session_names(self):
        proc = base.tmux("list-sessions", "-F", "#{session_name}", check=False)
        return {line.strip() for line in proc.stdout.splitlines()} if proc.returncode == 0 else set()

    def _end(self, task_id):
        status, body = self._request("POST", "/api/end-session", {
            "project": self.PROJECT, "taskId": task_id,
        })
        self.assertEqual(status, 200, body)
        name = spawn.session_name(self.PROJECT, task_id)
        base.wait_until(lambda: name not in self._session_names(),
                        message="ended probe session is still live")

    def _reviewed(self, task_id):
        status, preview = self._request(
            "GET", f"/api/discard-preview?project={self.PROJECT}&task={task_id}")
        self.assertEqual(status, 200, preview)
        return {
            "project": self.PROJECT, "taskId": task_id,
            "expectedBranchTip": preview["branchTip"],
            "expectedDirtyPaths": preview["dirtyPaths"],
        }

    def _assert_targets_intact(self):
        with open(os.path.join(self.venv, "bin", "python"), encoding="utf-8") as f:
            self.assertEqual(f.read(), "main interpreter sentinel\n")
        with open(self.env_file, encoding="utf-8") as f:
            self.assertEqual(f.read(), "MAIN_ONLY=1\n")

    def _assert_links(self, task_id):
        wt_dir = spawn.worktree_dir(self.config, self.PROJECT, task_id)
        self.assertEqual(os.readlink(os.path.join(wt_dir, ".venv")), self.venv)
        self.assertEqual(os.readlink(os.path.join(wt_dir, ".env.local")), self.env_file)
        self.assertEqual(self._git("status", "--porcelain", cwd=wt_dir).stdout, "")
        return wt_dir

    def test_links_are_ignored_even_after_git_add_all_and_a_commit(self):
        task_id = self._create_task("Links stay out of commits")
        result = self._spawn(task_id)
        self.assertNotIn("warnings", result)
        wt_dir = self._assert_links(task_id)
        exclude_path = os.path.join(self.repo_path, ".git", "info", "exclude")
        with open(exclude_path, encoding="utf-8") as f:
            self.assertTrue({"/.venv", "/.env.local"}.issubset(set(f.read().splitlines())))

        with open(os.path.join(wt_dir, "work.txt"), "w", encoding="utf-8") as f:
            f.write("committable work\n")
        self._git("add", "-A", cwd=wt_dir)
        self.assertEqual(self._git("diff", "--cached", "--name-only", cwd=wt_dir).stdout.strip(),
                         "work.txt")
        self._git("commit", "-q", "-m", "work without linked paths", cwd=wt_dir)
        self.assertEqual(self._git("show", "--format=", "--name-only", "HEAD", cwd=wt_dir).stdout.strip(),
                         "work.txt")
        self._assert_targets_intact()

    def test_discard_abandon_and_cleanup_remove_links_but_keep_main_targets(self):
        for route in ("/api/discard-attempt", "/api/abandon-worktree", "/api/cleanup-branch"):
            with self.subTest(route=route):
                task_id = self._create_task(f"Destroy links via {route}")
                self._spawn(task_id)
                wt_dir = self._assert_links(task_id)
                self._end(task_id)

                if route == "/api/cleanup-branch":
                    base.run(["backlog", "task", "edit", task_id, "-s", "Done", "--check-ac", "1",
                              "--plain"], cwd=wt_dir)
                    self._git("add", "backlog", cwd=wt_dir)
                    self._git("commit", "-q", "-m", f"finish {task_id}", cwd=wt_dir)
                    self._git("merge", "--no-ff", "-q", "-m", f"merge {task_id}",
                              spawn.branch_name(task_id))
                    payload = {"project": self.PROJECT, "taskId": task_id}
                else:
                    payload = self._reviewed(task_id)

                status, body = self._request("POST", route, payload)
                self.assertEqual(status, 200, body)
                self.assertTrue(body["worktreeRemoved"])
                self.assertFalse(os.path.lexists(os.path.join(wt_dir, ".venv")))
                self.assertFalse(os.path.lexists(os.path.join(wt_dir, ".env.local")))
                self._assert_targets_intact()

    def test_harvest_merges_finished_task_and_keeps_main_link_targets(self):
        task_id = self._create_task("Harvest linked worktree")
        self._spawn(task_id)
        wt_dir = self._assert_links(task_id)
        self._end(task_id)

        with open(os.path.join(wt_dir, "work.txt"), "w", encoding="utf-8") as f:
            f.write("finished\n")
        base.run(["backlog", "task", "edit", task_id, "-s", "Done", "--check-ac", "1",
                  "--plain"], cwd=wt_dir)
        self._git("add", "-A", cwd=wt_dir)
        self._git("commit", "-q", "-m", f"finish {task_id}", cwd=wt_dir)

        status, report = self._request("POST", "/api/harvest", {
            "project": self.PROJECT, "taskId": task_id,
        })
        self.assertEqual(status, 200, report)
        self.assertTrue(report["merged"], report)
        self.assertFalse(os.path.lexists(os.path.join(wt_dir, ".venv")))
        self.assertFalse(os.path.lexists(os.path.join(wt_dir, ".env.local")))
        self.assertFalse(os.path.isdir(wt_dir))
        self.assertEqual(self._git("branch", "--list", spawn.branch_name(task_id)).stdout.strip(), "")
        with open(os.path.join(self.repo_path, "work.txt"), encoding="utf-8") as f:
            self.assertEqual(f.read(), "finished\n")
        self._assert_targets_intact()

    def test_cap_uses_live_tmux_sessions_and_clears_after_end_session(self):
        self.project["maxAgents"] = 1
        first = self._create_task("First capped agent")
        second = self._create_task("Second capped agent")
        launched = self._spawn(first)
        status, body = self._request("POST", "/api/spawn", {
            "project": self.PROJECT, "taskId": second,
        })
        self.assertEqual(status, 409, body)
        self.assertIn(launched["session"], body["error"])
        self.assertFalse(os.path.isdir(spawn.worktree_dir(self.config, self.PROJECT, second)))
        self._end(first)
        self._spawn(second)

    def test_effective_gitignore_negation_warns_and_skips_unsafe_link(self):
        with open(os.path.join(self.repo_path, ".gitignore"), "w", encoding="utf-8") as f:
            f.write("!/.venv\n")
        self._git("add", ".gitignore")
        self._git("commit", "-q", "-m", "negate venv ignore")
        task_id = self._create_task("Do not create committable link")
        result = self._spawn(task_id)
        wt_dir = spawn.worktree_dir(self.config, self.PROJECT, task_id)
        self.assertFalse(os.path.lexists(os.path.join(wt_dir, ".venv")))
        self.assertIn(".venv", " ".join(result.get("warnings", [])))
        self.assertEqual(os.readlink(os.path.join(wt_dir, ".env.local")), self.env_file)
        self._assert_targets_intact()

    def test_missing_source_warns_while_existing_links_and_spawn_succeed(self):
        self.project["worktreeLinks"].append("missing.local")
        task_id = self._create_task("Missing optional link")
        result = self._spawn(task_id)
        wt_dir = self._assert_links(task_id)
        self.assertFalse(os.path.lexists(os.path.join(wt_dir, "missing.local")))
        self.assertIn("missing.local", " ".join(result.get("warnings", [])))
        self._assert_targets_intact()

    def test_linked_project_checkout_uses_common_git_info_exclude(self):
        original_repo = self.repo_path
        linked_checkout = os.path.join(self.tmp_dir, "linked-project-checkout")
        self._git("worktree", "add", "-q", "-b", "linked-main", linked_checkout, "main")
        self.assertTrue(os.path.isfile(os.path.join(linked_checkout, ".git")))
        self.repo_path = linked_checkout
        self.project["path"] = linked_checkout

        self.venv = os.path.join(linked_checkout, ".venv")
        os.makedirs(os.path.join(self.venv, "bin"))
        with open(os.path.join(self.venv, "bin", "python"), "w", encoding="utf-8") as f:
            f.write("main interpreter sentinel\n")
        self.env_file = os.path.join(linked_checkout, ".env.local")
        with open(self.env_file, "w", encoding="utf-8") as f:
            f.write("MAIN_ONLY=1\n")

        task_id = self._create_task("Spawn from linked project checkout")
        self._spawn(task_id)
        wt_dir = self._assert_links(task_id)
        common_exclude = os.path.join(original_repo, ".git", "info", "exclude")
        resolved = self._git("rev-parse", "--git-path", "info/exclude").stdout.strip()
        self.assertEqual(os.path.realpath(os.path.join(linked_checkout, resolved)),
                         os.path.realpath(common_exclude))
        with open(common_exclude, encoding="utf-8") as f:
            self.assertTrue({"/.venv", "/.env.local"}.issubset(set(f.read().splitlines())))
        with open(os.path.join(wt_dir, "work.txt"), "w", encoding="utf-8") as f:
            f.write("real work\n")
        self._git("add", "-A", cwd=wt_dir)
        self.assertEqual(self._git("diff", "--cached", "--name-only", cwd=wt_dir).stdout.strip(),
                         "work.txt")
        self._assert_targets_intact()
