"""Integration coverage for spawn.py's real spawn flow: a real git
worktree, a real tmux session (running a harmless probe command via
CENTRALE_SPAWN_CMD, never a real agent), the pre-worktree claim commit,
the duplicate-session 409, and re-spawn reusing an existing worktree and
branch after its session was killed.

Run explicitly: python3 -m unittest tests_integration.test_spawn_integration
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
class SpawnIntegrationTests(base.IntegrationCase):
    def setUp(self):
        super().setUp()
        self.repo_path = base.init_backlog_repo(self.tmp_dir, name="spawnproj")
        self.task_id = base.create_task(
            self.repo_path, "Spawnable task", description="a task for spawn integration tests"
        )
        self.worktree_root = os.path.join(self.tmp_dir, "worktrees")
        self.config = {
            "worktreeRoot": self.worktree_root,
            "projects": [{"name": "spawnproj", "path": self.repo_path, "checkCommand": None}],
            "agents": server.normalize_agents_map({"claude": ["claude"]}),
            "defaultAgent": "claude",
            "capabilities": {"tmux": True},
        }
        patcher = mock.patch.dict(os.environ, {"CENTRALE_SPAWN_CMD": base.probe_spawn_cmd()})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _session_names(self):
        proc = base.tmux("list-sessions", "-F", "#{session_name}", check=False)
        if proc.returncode != 0:
            return set()
        return {line.strip() for line in proc.stdout.splitlines() if line.strip()}

    def test_spawn_creates_worktree_tmux_session_and_claims_task(self):
        result = spawn.spawn(self.config, "spawnproj", self.task_id)
        name = self.track_session(result["session"])

        self.assertEqual(name, spawn.session_name("spawnproj", self.task_id))
        self.assertEqual(result["attach"], f"tmux attach -t {name}")
        self.assertEqual(result["agent"], "custom")  # CENTRALE_SPAWN_CMD overrides agent selection

        # Real tmux session actually exists on the sandboxed socket.
        base.wait_until(lambda: name in self._session_names(), timeout=10.0,
                         message="spawned tmux session never appeared")

        # Real git worktree, on the expected branch.
        wt_dir = spawn.worktree_dir(self.config, "spawnproj", self.task_id)
        self.assertTrue(os.path.isdir(wt_dir))
        branch_proc = base.run(["git", "branch", "--show-current"], cwd=wt_dir)
        self.assertEqual(branch_proc.stdout.strip(), spawn.branch_name(self.task_id))

        # The claim + commit landed on the *main* repo before the worktree
        # was cut: status is In Progress, and there's a "backlog: claim"
        # commit for it on the repo's own current branch.
        view = base.run(["backlog", "task", "view", self.task_id, "--json"], cwd=self.repo_path)
        import json
        task_data = json.loads(view.stdout)["task"]
        self.assertEqual(task_data["status"], "In Progress")

        log_proc = base.run(["git", "log", "-1", "--format=%s"], cwd=self.repo_path)
        self.assertEqual(log_proc.stdout.strip(), f"backlog: claim {self.task_id} for spawn")

        main_status = base.run(["git", "status", "--porcelain"], cwd=self.repo_path)
        self.assertEqual(main_status.stdout.strip(), "", "main checkout should be clean after the claim commit")

    def test_duplicate_spawn_is_rejected_with_409(self):
        result = spawn.spawn(self.config, "spawnproj", self.task_id)
        self.track_session(result["session"])
        base.wait_until(lambda: result["session"] in self._session_names(), timeout=10.0)

        with self.assertRaises(spawn.SpawnError) as ctx:
            spawn.spawn(self.config, "spawnproj", self.task_id)
        self.assertEqual(ctx.exception.status, 409)

    def test_kill_session_then_respawn_reuses_worktree_and_branch(self):
        first = spawn.spawn(self.config, "spawnproj", self.task_id)
        name = first["session"]
        base.wait_until(lambda: name in self._session_names(), timeout=10.0)

        wt_dir = spawn.worktree_dir(self.config, "spawnproj", self.task_id)
        wt_real = os.path.realpath(wt_dir)

        base.tmux("kill-session", "-t", name, check=True)
        base.wait_until(lambda: name not in self._session_names(), timeout=10.0,
                         message="killed session still showing as live")

        second = spawn.spawn(self.config, "spawnproj", self.task_id)
        self.track_session(second["session"])
        self.assertEqual(second["session"], name)
        base.wait_until(lambda: name in self._session_names(), timeout=10.0,
                         message="re-spawned tmux session never appeared")

        # Same worktree directory reused, not a fresh one.
        self.assertEqual(os.path.realpath(spawn.worktree_dir(self.config, "spawnproj", self.task_id)), wt_real)

        # Still exactly one task/<id> branch -- re-spawn didn't create a
        # second, divergent one.
        branches = base.run(["git", "branch", "--list", spawn.branch_name(self.task_id)], cwd=self.repo_path)
        self.assertEqual(len(branches.stdout.strip().splitlines()), 1)


@base.require_tools("git", "tmux", "backlog")
class InRepoWorktreeSpawnIntegrationTests(base.IntegrationCase):
    """Task-33: worktreeRoot "@repo" -- a spawn's worktree lives inside
    the project it belongs to (instead of a shared sibling directory),
    so a spawned codex agent inherits the project's own filesystem-
    ancestry directory trust. This is the real-process proof that the
    .git/info/exclude entry _ensure_worktree writes actually keeps
    `git status` clean in the real repo, not just that the file gets an
    extra line (already covered hermetically in tests/test_spawn.py)."""

    def setUp(self):
        super().setUp()
        self.repo_path = base.init_backlog_repo(self.tmp_dir, name="inrepoproj")
        self.task_id = base.create_task(
            self.repo_path, "In-repo worktree task", description="task-33 integration coverage"
        )
        self.config = {
            "worktreeRoot": "@repo",
            "projects": [{"name": "inrepoproj", "path": self.repo_path, "checkCommand": None}],
            "agents": server.normalize_agents_map({"claude": ["claude"]}),
            "defaultAgent": "claude",
            "capabilities": {"tmux": True},
        }
        patcher = mock.patch.dict(os.environ, {"CENTRALE_SPAWN_CMD": base.probe_spawn_cmd()})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _session_names(self):
        proc = base.tmux("list-sessions", "-F", "#{session_name}", check=False)
        if proc.returncode != 0:
            return set()
        return {line.strip() for line in proc.stdout.splitlines() if line.strip()}

    def test_worktree_lands_inside_the_repo_and_git_status_stays_clean(self):
        result = spawn.spawn(self.config, "inrepoproj", self.task_id)
        self.track_session(result["session"])
        base.wait_until(lambda: result["session"] in self._session_names(), timeout=10.0,
                         message="spawned tmux session never appeared")

        wt_dir = spawn.worktree_dir(self.config, "inrepoproj", self.task_id)
        # The worktree is really inside the repo, not a sibling directory.
        self.assertTrue(os.path.realpath(wt_dir).startswith(os.path.realpath(self.repo_path) + os.sep))
        self.assertTrue(os.path.isdir(wt_dir))

        # .git/info/exclude picked up the entry (untracked-only -- never
        # the tracked .gitignore).
        exclude_path = os.path.join(self.repo_path, ".git", "info", "exclude")
        with open(exclude_path, "r", encoding="utf-8") as f:
            self.assertIn(".centrale-worktrees/", f.read().splitlines())

        # And the real proof: `git status` in the main repo is clean even
        # though a whole extra worktree directory now lives inside it.
        main_status = base.run(["git", "status", "--porcelain"], cwd=self.repo_path)
        self.assertEqual(
            main_status.stdout.strip(), "",
            "an in-repo worktree root must not show up in the main repo's git status",
        )


if __name__ == "__main__":
    unittest.main()


@base.require_tools("git", "tmux", "backlog")
class AttachedClientSessionGeometryIntegrationTests(base.IntegrationCase):
    """task-152: a real spawn onto a tmux server that has a client
    attached lands at spawn.SESSION_GEOMETRY.

    This is the condition the original geometry work could not see.
    `window-size` defaults to `latest` and is inherited globally, so it
    is resolved when a session is BORN -- against the most recently
    attached client, even for a detached `new-session`. On a clean
    socket there is no client, `-x/-y` survives, and every test passes;
    on the owner's machine a terminal is always attached to the same
    server, so the geometry was overridden at creation and the
    `window-size manual` pin then froze the wrong size. Measured on an
    isolated socket with a 142x30 client attached: `new-session -x 220
    -y 50` gave 142x29, the pin held 142x29, and only an explicit
    `resize-window` behind the pin reached 220x50.

    So these tests attach a real client first, and drive spawn.spawn()
    itself -- real git worktree, real claim commit, real tmux session
    (a harmless sleep probe, never an agent) -- rather than issuing the
    tmux commands by hand, which is what made a clean-socket test look
    like proof.
    """

    # task-161: named off this run's socket, not a fixed
    # "centrale-itest-host". Session names are scoped to a socket, so the
    # per-run socket already separates two runs' host sessions -- but a
    # name that reads as unique and is not invites exactly the sharing
    # this tier is not allowed to do, and a stray session in a diagnostic
    # `list-sessions` should name the run it belongs to.
    HOST_SESSION = f"{base.TMUX_SOCKET}-host"
    CLIENT_SIZE = (142, 30)

    def setUp(self):
        super().setUp()
        self.repo_path = base.init_backlog_repo(self.tmp_dir, name="geomproj")
        self.task_id = base.create_task(
            self.repo_path, "Geometry task", description="task-152 integration coverage"
        )
        self.config = {
            "worktreeRoot": os.path.join(self.tmp_dir, "worktrees"),
            "projects": [{"name": "geomproj", "path": self.repo_path, "checkCommand": None}],
            "agents": server.normalize_agents_map({"claude": ["claude"]}),
            "defaultAgent": "claude",
            "capabilities": {"tmux": True},
        }
        patcher = mock.patch.dict(os.environ, {"CENTRALE_SPAWN_CMD": base.probe_spawn_cmd()})
        patcher.start()
        self.addCleanup(patcher.stop)

        # Something for a client to attach to: a session of its own, at a
        # size that is neither the client's nor SESSION_GEOMETRY, so
        # nothing here can accidentally agree with the answer.
        base.tmux("new-session", "-d", "-s", self.HOST_SESSION, "-x", "100", "-y", "40",
                  'sh -c "sleep 300"', check=True)
        self.track_session(self.HOST_SESSION)
        columns, rows = self.CLIENT_SIZE
        self.attach_client(self.HOST_SESSION, columns=columns, rows=rows)
        # The premise of every test below. Without it they prove nothing
        # -- which is exactly how this bug shipped.
        self.assertIn(f"{columns}x{rows}", base.attached_client_sizes(),
                      "no client attached: this test would pass for the wrong reason")

    def _session_names(self):
        proc = base.tmux("list-sessions", "-F", "#{session_name}", check=False)
        if proc.returncode != 0:
            return set()
        return {line.strip() for line in proc.stdout.splitlines() if line.strip()}

    def _geometry(self, name):
        proc = base.tmux("display-message", "-p", "-t", f"={name}:",
                         "#{window_width}x#{window_height}", check=True)
        return proc.stdout.strip()

    def _spawn_and_wait(self):
        result = spawn.spawn(self.config, "geomproj", self.task_id)
        name = self.track_session(result["session"])
        base.wait_until(lambda: name in self._session_names(), timeout=10.0,
                        message="spawned tmux session never appeared")
        return name

    def test_a_real_spawn_lands_at_the_geometry_with_a_client_attached(self):
        name = self._spawn_and_wait()
        columns, rows = spawn.SESSION_GEOMETRY
        self.assertEqual(
            self._geometry(name), f"{columns}x{rows}",
            "the spawned session did not get SESSION_GEOMETRY while a client was "
            "attached -- the birth-time window-size override is back")

    def test_the_geometry_survives_a_later_attach_and_detach(self):
        # The other half of the mechanism, and the reason the pin stays:
        # `window-size latest` would let the first client that attaches
        # renegotiate the window down to its own terminal, and it stays
        # there after that client detaches.
        name = self._spawn_and_wait()
        columns, rows = spawn.SESSION_GEOMETRY
        client = self.attach_client(name, columns=self.CLIENT_SIZE[0], rows=self.CLIENT_SIZE[1])
        self.assertEqual(self._geometry(name), f"{columns}x{rows}",
                         "an attached client shrank the pinned session")
        base.terminate_and_wait(client)
        # Down to just setUp's host client again: the one that mattered
        # for this assertion is really gone before it is made.
        base.wait_until(lambda: len(base.attached_client_sizes()) == 1, timeout=5.0,
                        message="the second client never detached")
        self.assertEqual(self._geometry(name), f"{columns}x{rows}",
                         "the session shrank after a client detached")

    def test_a_capture_can_actually_return_the_theater_s_full_window(self):
        # AC #4 made SESSION_GEOMETRY's rows the capture ceiling itself,
        # so the claim is now testable rather than merely documented: a
        # pane painted to its full height hands back every line the
        # theater asks for. An interactive shell as the probe command,
        # since this one needs to type into the pane the spawn created
        # -- never a real agent, and it dies with the session.
        with mock.patch.dict(os.environ, {"CENTRALE_SPAWN_CMD": 'sh -c "exec sh -i" itest-probe'}):
            name = self._spawn_and_wait()
        columns, rows = spawn.SESSION_GEOMETRY
        self.assertEqual(rows, server.MAX_SESSION_PANE_LINES)

        # Paint the whole pane the way an agent does -- on the ALTERNATE
        # SCREEN. Without that the shell scrolls into real scrollback and
        # `-S -200` would return 200 lines from a pane of any height at
        # all, which is the reading that let a 50-row pane look like it
        # was answering the theater's 200-line request.
        base.tmux("send-keys", "-t", f"={name}:",
                  r'printf "\033[?1049h"; i=1; while [ $i -le %d ]; do echo "line$i"; i=$((i+1)); done'
                  % (rows * 2),
                  "Enter", check=True)
        base.wait_until(
            lambda: len(server.capture_session_pane(name, server.MAX_SESSION_PANE_LINES)) == rows,
            timeout=15.0,
            message="a full-height alternate-screen pane did not return "
                    "MAX_SESSION_PANE_LINES lines")
        # And the pane really had no scrollback to supply them from.
        hist = base.tmux("display-message", "-p", "-t", f"={name}:", "#{history_size}", check=True)
        self.assertEqual(hist.stdout.strip(), "0")
