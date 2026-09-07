"""Integration coverage for spawn.resume(): resuming an interrupted
agent (a real dirty worktree, no live session) with a real tmux session
running either a configured `resumeCmd` override or, lacking one, a
fresh start carrying RESUME_FALLBACK_NOTE -- never a real agent.

Run explicitly: python3 -m unittest tests_integration.test_resume_integration
"""

from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

from tests_integration import base

sys.path.insert(0, base.CENTRALE_ROOT)
import server  # noqa: E402
import spawn  # noqa: E402


def tearDownModule():
    base.assert_test_tmux_footprint_gone()


@base.require_tools("git", "tmux", "backlog")
class ResumeIntegrationTests(base.IntegrationCase):
    def setUp(self):
        super().setUp()
        self.repo_path = base.init_backlog_repo(self.tmp_dir, name="resumeproj")
        self.worktree_root = os.path.join(self.tmp_dir, "worktrees")
        self.project = {"name": "resumeproj", "path": self.repo_path, "checkCommand": None}

    def _spawn_then_interrupt(self, task_id, config):
        """Real spawn (via the harmless CENTRALE_SPAWN_CMD override), then
        simulates an interrupted session: kills the tmux session but
        leaves real uncommitted work behind in the worktree -- the exact
        "dirty worktree, no live session" state resume() exists for."""
        with mock.patch.dict(os.environ, {"CENTRALE_SPAWN_CMD": base.probe_spawn_cmd(marker="itest-initial")}):
            spawned = spawn.spawn(config, "resumeproj", task_id)
        name = spawned["session"]
        base.wait_until(lambda: self._session_alive(name), timeout=10.0,
                         message="initial spawn session never appeared")

        base.tmux("kill-session", "-t", name, check=True)
        base.wait_until(lambda: not self._session_alive(name), timeout=10.0,
                         message="initial session never died")

        wt_dir = spawn.worktree_dir(config, "resumeproj", task_id)
        with open(os.path.join(wt_dir, "leftover.txt"), "w", encoding="utf-8") as f:
            f.write("uncommitted work from the interrupted session\n")
        dirty = base.run(["git", "status", "--porcelain"], cwd=wt_dir)
        self.assertTrue(dirty.stdout.strip(), "worktree should be dirty before resume()")
        return wt_dir

    def _session_alive(self, name):
        proc = base.tmux("list-sessions", "-F", "#{session_name}", check=False)
        if proc.returncode != 0:
            return False
        return name in {line.strip() for line in proc.stdout.splitlines() if line.strip()}

    def test_resume_dirty_worktree_uses_configured_resume_cmd(self):
        config = {
            "worktreeRoot": self.worktree_root,
            "projects": [self.project],
            "agents": server.normalize_agents_map({
                "claude": ["claude"],
                "probe-agent": {"cmd": ["sleep", "9999"], "resumeCmd": ["sleep", "266"]},
            }),
            "defaultAgent": "claude",
            "capabilities": {"tmux": True},
        }
        task_id = base.create_task(self.repo_path, "Resumable task", assignee="probe-agent")
        wt_dir = self._spawn_then_interrupt(task_id, config)
        wt_real = os.path.realpath(wt_dir)

        result = spawn.resume(config, "resumeproj", task_id)
        name = self.track_session(result["session"])

        self.assertTrue(result["resumed"])
        self.assertEqual(result["agent"], "probe-agent")
        base.wait_until(lambda: self._session_alive(name), timeout=10.0,
                         message="resumed session never appeared")

        # Real proof the *configured resumeCmd* launched (["sleep",
        # "266"]), not the agent's regular `cmd` (sleep 9999) and not a
        # prompt-carrying fallback -- 266 is a marker duration used only
        # here.
        pid = base.pane_pid(name)
        base.wait_until(lambda: "266" in base.read_proc_cmdline(pid), timeout=5.0,
                         message=f"resumed pane wasn't running the configured resumeCmd: {base.read_proc_cmdline(pid)}")

        # Same worktree reused, still dirty -- resume() never re-claims
        # or re-commits (see spawn.resume's own docstring).
        self.assertEqual(os.path.realpath(spawn.worktree_dir(config, "resumeproj", task_id)), wt_real)
        still_dirty = base.run(["git", "status", "--porcelain"], cwd=wt_dir)
        self.assertIn("leftover.txt", still_dirty.stdout)

    def test_resume_without_resume_cmd_falls_back_to_fresh_start(self):
        # No resumeCmd configured, and cmd[0] != "claude" -- priority
        # branch 3: a fresh start with the agent's own cmd plus
        # RESUME_FALLBACK_NOTE appended to the standard prompt.
        config = {
            "worktreeRoot": self.worktree_root,
            "projects": [self.project],
            "agents": server.normalize_agents_map({
                "claude": ["claude"],
                "no-resume-agent": {"cmd": ["sh", "-c", "sleep 245", "itest-fallback"]},
            }),
            "defaultAgent": "claude",
            "capabilities": {"tmux": True},
        }
        task_id = base.create_task(self.repo_path, "Fallback task", assignee="no-resume-agent")
        self._spawn_then_interrupt(task_id, config)

        result = spawn.resume(config, "resumeproj", task_id)
        name = self.track_session(result["session"])

        self.assertTrue(result["resumed"])
        self.assertEqual(result["agent"], "no-resume-agent")
        base.wait_until(lambda: self._session_alive(name), timeout=10.0)

        pid = base.pane_pid(name)
        base.wait_until(lambda: any("245" in part for part in base.read_proc_cmdline(pid)), timeout=5.0,
                         message=f"fallback pane wasn't running the agent's own cmd: {base.read_proc_cmdline(pid)}")
        # And, unlike the configured-resumeCmd branch, a prompt argument
        # *was* appended (RESUME_FALLBACK_NOTE folded into it) -- this is
        # branch 3 of resume()'s priority list, a fresh start, not a
        # silent resumeCmd continuation.
        full_cmdline = base.read_proc_cmdline(pid)
        self.assertTrue(any("uncommitted changes left behind" in part for part in full_cmdline),
                         f"expected RESUME_FALLBACK_NOTE in the launched prompt: {full_cmdline}")

    def test_resume_duplicate_session_conflicts_with_409(self):
        config = {
            "worktreeRoot": self.worktree_root,
            "projects": [self.project],
            "agents": server.normalize_agents_map({
                "claude": ["claude"],
                "probe-agent": {"cmd": ["sleep", "9999"], "resumeCmd": ["sleep", "266"]},
            }),
            "defaultAgent": "claude",
            "capabilities": {"tmux": True},
        }
        task_id = base.create_task(self.repo_path, "Resumable task 2", assignee="probe-agent")
        self._spawn_then_interrupt(task_id, config)

        result = spawn.resume(config, "resumeproj", task_id)
        self.track_session(result["session"])
        base.wait_until(lambda: self._session_alive(result["session"]), timeout=10.0)

        with self.assertRaises(spawn.SpawnError) as ctx:
            spawn.resume(config, "resumeproj", task_id)
        self.assertEqual(ctx.exception.status, 409)


if __name__ == "__main__":
    unittest.main()
