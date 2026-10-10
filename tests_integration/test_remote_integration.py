"""Integration coverage for the life of a remote task (doc-3, task-207):
spawn, End session (linked windows and the orphan sweep), resume, owed
teardowns and cleanup, with every worker script run for real -- and
(task-224) one project with a task on the hub and one on the worker at
once, each located from git and both merged.

Two targets:

- LoopbackWorkerTests (always, given git/tmux/backlog/python3): the
  "worker" is this machine under a separate HOME. machines.run_ssh is
  replaced by a local `sh -c` of the exact command line ssh would carry,
  so the generated scripts, POSIX quoting, the worker's own `~`
  expansion, real git (parked repository, worktrees, push and fetch), real
  tmux (this run's -L socket, through the shim) and real processes are
  all exercised; only the network hop is not.
- SshTargetTests (only when CENTRALE_TEST_SSH_TARGET names an ssh
  target, e.g. a disposable sshd container or VM): the same flow over
  real ssh. Its machine names a tmuxSocket (task-209), so the worker's
  own tmux server is never touched, and it works under
  CENTRALE_ITEST_SSH_ROOT (default ~/centrale-itest-<pid>) there,
  removing it afterwards.

No agent is ever launched: CENTRALE_SPAWN_CMD runs a probe that starts
`nohup` and `setsid` children in its worktree -- the leftovers the sweep
must kill -- and a stand-in for Codex's app-server daemon runs with the
worktree as its cwd, which the sweep must spare.

Run explicitly: python3 -m unittest tests_integration.test_remote_integration
"""

from __future__ import annotations

import os
import secrets
import shutil
import subprocess
import sys
import time
import unittest
from unittest import mock

from tests_integration import base

sys.path.insert(0, base.CENTRALE_ROOT)
import harvest  # noqa: E402
import machines  # noqa: E402
import remote  # noqa: E402
import remote_harvest  # noqa: E402
import remote_status  # noqa: E402
import server  # noqa: E402
import spawn  # noqa: E402

SSH_TARGET_ENV = "CENTRALE_TEST_SSH_TARGET"
SSH_ROOT_ENV = "CENTRALE_ITEST_SSH_ROOT"


@base.require_tools("git")
class ParkedRepositoryMigrationTests(base.IntegrationCase):
    def test_trust_probe_reads_worker_tool_settings_without_writing_them(self):
        import json
        repo = os.path.join(self.tmp_dir, "repos", "project")
        codex_home = os.path.join(self.tmp_dir, "codex")
        os.makedirs(codex_home)
        with open(os.path.join(codex_home, "config.toml"), "w", encoding="utf-8") as handle:
            handle.write(f'[projects."{repo}"]\ntrust_level = "trusted"\n')
        with open(os.path.join(self.tmp_dir, ".claude.json"), "w", encoding="utf-8") as handle:
            json.dump({"projects": {repo: {"hasTrustDialogAccepted": True}}}, handle)
        env = dict(os.environ, HOME=self.tmp_dir, CODEX_HOME=codex_home)
        for kind in ("codex", "claude"):
            args = ["python3", "-c", machines._TRUST_PROBE, repo, kind]
            self.assertEqual(base.run(args, env=env).stdout.strip(), "trusted")
            self.assertEqual(base.run(args[:-2] + [repo + "-other", kind], env=env).stdout.strip(),
                             "untrusted")

    def test_repair_preserves_checked_out_task_and_accepts_later_main_push(self):
        source = os.path.join(self.tmp_dir, "source")
        project = os.path.join(self.tmp_dir, "repos", "project")
        old = project + ".git"
        task = os.path.join(self.tmp_dir, "work", "project-task-1")
        os.makedirs(project)
        base.run(["git", "init", "-q", "-b", "main", source])
        base.run(["git", "-c", "user.name=Probe", "-c", "user.email=probe@example.invalid",
                  "commit", "-qm", "base", "--allow-empty"], cwd=source)
        base.run(["git", "init", "-q", "--bare", "-b", "main", old])
        base.run(["git", "push", "-q", old, "main"], cwd=source)
        base.run(["git", "--git-dir", old, "worktree", "add", "-q", "-b", "task/1", task, "main"])
        probe = ["sh", "-c", machines._REPO_PROBE, "sh", project, old]
        self.assertEqual(base.run(probe).stdout.strip(), "legacy")
        with open(os.path.join(task, "uncommitted"), "w", encoding="utf-8") as handle:
            handle.write("keep me")
        os.rename(old, os.path.join(project, ".git"))
        base.run(["git", "config", "core.bare", "false"], cwd=project)
        base.run(["git", "worktree", "repair", task], cwd=project)
        base.run(["git", "checkout", "-q", "--detach", "main"], cwd=project)
        self.assertEqual(base.run(probe).stdout.strip(), "parked")
        self.assertEqual(base.run(["git", "symbolic-ref", "-q", "HEAD"], cwd=project,
                                  check=False).returncode, 1)
        self.assertIn("?? uncommitted", base.run(["git", "status", "--short"], cwd=task).stdout)
        base.run(["git", "-c", "user.name=Probe", "-c", "user.email=probe@example.invalid",
                  "commit", "-qm", "next", "--allow-empty"], cwd=source)
        base.run(["git", "push", "-q", os.path.join(project, ".git"), "main"], cwd=source)
        self.assertEqual(base.run(["git", "rev-parse", "main"], cwd=project).stdout,
                         base.run(["git", "rev-parse", "main"], cwd=source).stdout)
        self.assertIn("branch refs/heads/task/1",
                      base.run(["git", "worktree", "list", "--porcelain"], cwd=project).stdout)

# The agent stand-in: two children that outlive kill-session (doc-3: nohup
# and setsid survive it), then the pane's own long sleep. The trailing
# word is $0 for the prompt Centrale appends.
PROBE = ("sh -c 'nohup sleep 307 >/dev/null 2>&1 & setsid sleep 308 >/dev/null 2>&1 & "
         "exec sleep 309' centrale-itest-207")


# Whole command lines, anchored: the tmux server keeps the argv of the
# client that started it, probe script included, so an unanchored match
# would count (and kill) this run's tmux server too.
ORPHANS = r"^sleep 30[78]$"
PROBE_PROCESSES = r"^sleep 30[789]$"


# task-208's checkCommand stand-in: a plain background child (dies with
# timeout's process group), a setsid one that escapes the group (the
# scratch worktree's sweep must get it) and the foreground sleep timeout
# kills.
HUNG_CHECK = "sleep 311 & setsid sleep 312 >/dev/null 2>&1 & exec sleep 313"
CHECK_PROCESSES = r"^sleep 31[123]$"


def tearDownModule():
    base.assert_test_tmux_footprint_gone()


def _pids_running(pattern):
    """Pids whose command line matches `pattern` (a regex), read from /proc."""
    import re
    found = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/cmdline", "rb") as f:
                cmdline = f.read().replace(b"\0", b" ").decode(errors="replace").strip()
        except OSError:
            continue
        if re.search(pattern, cmdline):
            found.append(int(entry))
    return found


@base.require_tools("git", "tmux", "backlog", "python3")
class LoopbackWorkerTests(base.IntegrationCase):
    PROJECT = "remoteproj"

    def setUp(self):
        super().setUp()
        self.hub_repo = base.init_backlog_repo(self.tmp_dir, name=self.PROJECT)
        self.task_id = base.create_task(self.hub_repo, "Remote probe task", status="To Do")
        base.run(["git", "add", "-A"], cwd=self.hub_repo)
        base.run(["git", "commit", "-q", "-m", "task"], cwd=self.hub_repo)
        self.worker_home = os.path.join(self.tmp_dir, "worker-home")
        self.worker_tmp = os.path.join(self.worker_home, "tmp")
        os.makedirs(self.worker_tmp)
        os.makedirs(os.path.join(self.worker_home, "repos", self.PROJECT, ".venv"))
        worker_repo = os.path.join(self.worker_home, "repos", self.PROJECT)
        base.run(["git", "init", "-q", "-b", "parking", worker_repo])
        base.run(["git", "-c", "user.name=Probe", "-c", "user.email=probe@example.invalid",
                  "commit", "-qm", "parked", "--allow-empty"], cwd=worker_repo)
        base.run(["git", "checkout", "-q", "--detach", "HEAD"], cwd=worker_repo)
        self.machine = {"name": "loop", "ssh": "loop-target", "repoRoot": "~/repos",
                        "worktreeRoot": "~/work", "maxAgents": None}
        self.config = {
            "machines": [self.machine],
            "projects": [{"name": self.PROJECT, "path": self.hub_repo, "machines": ["loop"],
                          "defaultMachine": "loop", "maxAgents": None, "worktreeLinks": [".venv"]}],
            "worktreeRoot": os.path.join(self.tmp_dir, "hub-worktrees"),
            "lockSpawnedTaskFiles": False,
            "refreshIntervalSeconds": 10,
        }
        self.project = self.config["projects"][0]
        self.wt = os.path.join(self.worker_home, "work", f"{self.PROJECT}-{self.task_id.lower()}")
        self.unreachable = False
        self.worker_login_path = None
        for patcher in (
            mock.patch.object(machines, "run_ssh", self.loopback_ssh),
            mock.patch.object(machines, "ssh_command", lambda: ["ssh", "-F", "/dev/null"]),
            mock.patch.object(remote, "git_url", self.local_url),
            mock.patch.object(remote, "_worker_hooks", lambda cmd, supports_hooks=None: []),
            mock.patch.dict(os.environ, {"CENTRALE_SPAWN_CMD": PROBE,
                                         remote.OWED_ENV: os.path.join(self.tmp_dir, "owed.json")}),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(self.kill_leftovers)

    # -- the loopback worker -------------------------------------------------

    def loopback_ssh(self, argv, input=None, timeout=None):
        if self.unreachable:
            return 255, "", "ssh: connect to host loop-target port 22: Connection timed out", False
        env = dict(os.environ, HOME=self.worker_home, TMPDIR=self.worker_tmp)
        if self.worker_login_path is not None:
            env["PATH"] = self.worker_login_path
        proc = subprocess.run(["sh", "-c", argv[-1]], input=input, capture_output=True, text=True,
                              timeout=60, env=env, cwd=self.worker_home)
        return proc.returncode, proc.stdout, proc.stderr, False

    def local_url(self, machine, path):
        return remote.expand(path, self.worker_home)

    def kill_leftovers(self):
        for pattern in (PROBE_PROCESSES, "centrale-itest-daemon", CHECK_PROCESSES):
            for pid in _pids_running(pattern):
                try:
                    os.kill(pid, 9)
                except OSError:
                    pass

    @property
    def placed(self):
        """The project as a task on the loopback worker sees it (task-224)."""
        return machines.placed(self.project, "loop")

    def session(self):
        return spawn.session_name(self.PROJECT, self.task_id)

    def session_alive(self):
        return base.tmux("has-session", "-t", f"={self.session()}").returncode == 0

    # -- the flow -------------------------------------------------------------

    def test_session_shell_finds_tools_from_interactive_worker_profile(self):
        tool_bin = os.path.join(self.worker_home, "agent-bin")
        login_bin = os.path.join(self.worker_home, "login-bin")
        for directory in (tool_bin, login_bin):
            os.makedirs(directory)
            for name in ("backlog", "claude", "codex"):
                tool = os.path.join(directory, name)
                with open(tool, "w", encoding="utf-8") as handle:
                    handle.write("#!/bin/sh\nexit 0\n")
                os.chmod(tool, 0o755)
        with open(os.path.join(self.worker_home, ".bashrc"), "w", encoding="utf-8") as handle:
            handle.write('export PATH="$HOME/agent-bin:$PATH"\n')
        # A login-PATH tool wins over the interactive directory appended by
        # Centrale. Put stand-ins first so the result is independent of the
        # real hub's npm, nvm or local CLI installation paths (task-232).
        self.worker_login_path = login_bin + os.pathsep + os.environ["PATH"]
        probe_file = os.path.join(self.worker_home, "tool-probe.sh")
        with open(probe_file, "w", encoding="utf-8") as handle:
            handle.write('#!/bin/sh\nprintf "PATH=%s\\n" "$PATH" > tools-found\n'
                         '{ command -v backlog; command -v claude; command -v codex; } >> tools-found\n'
                         'exec sleep 60\n')
        probe = f"sh {probe_file}"
        with mock.patch.dict(os.environ, {"CENTRALE_SPAWN_CMD": probe}):
            spawn.spawn(self.config, self.PROJECT, self.task_id)
        self.addCleanup(base.kill_test_tmux_session, self.session())
        expected = [os.path.join(login_bin, name) for name in ("backlog", "claude", "codex")]
        def tool_output():
            found = os.path.join(self.wt, "tools-found")
            if not os.path.exists(found):
                return False
            with open(found, encoding="utf-8") as handle:
                lines = handle.read().splitlines()
            return (len(lines) == 4 and lines[0].startswith("PATH=")
                    and tool_bin in lines[0][len("PATH="):].split(os.pathsep)
                    and lines[1:] == expected)
        try:
            base.wait_until(tool_output, message="non-interactive agent shell found worker tools")
        except AssertionError:
            found = os.path.join(self.wt, "tools-found")
            if os.path.exists(found):
                with open(found, encoding="utf-8") as handle:
                    actual = handle.read()
            else:
                actual = "missing"
            self.fail(f"tool output {actual!r}; expected {expected!r}")

    def test_spawn_end_session_resume_and_cleanup(self):
        result = spawn.spawn(self.config, self.PROJECT, self.task_id)
        self.assertEqual(result["machine"], "loop")
        self.assertTrue(self.session_alive())
        # Worktree cut in the worker's parked repository from the hub's claim commit.
        hub_main = base.run(["git", "rev-parse", "main"], cwd=self.hub_repo).stdout.strip()
        bare = os.path.join(self.worker_home, "repos", self.PROJECT, ".git")
        self.assertEqual(base.run(["git", "--git-dir", bare, "rev-parse", "main"]).stdout.strip(), hub_main)
        self.assertEqual(base.run(["git", "rev-parse", "HEAD"], cwd=self.wt).stdout.strip(), hub_main)
        link = os.path.join(self.wt, ".venv")
        self.assertTrue(os.path.islink(link))
        self.assertEqual(os.readlink(link), os.path.join(self.worker_home, "repos", self.PROJECT, ".venv"))
        self.assertEqual(base.run(["git", "status", "--porcelain"], cwd=self.wt).stdout, "")
        option = base.tmux("show-options", "-v", "-t", f"={self.session()}:", "@centrale_spawn")
        self.assertEqual(option.stdout.strip(), result["spawnId"])

        # A window linked into another session, as task-209's control session does.
        base.tmux("new-session", "-d", "-s", "centrale_control_itest", "sleep 600")
        self.addCleanup(base.kill_test_tmux_session, "centrale_control_itest")
        window = base.tmux("display-message", "-p", "-t", f"={self.session()}:", "#{window_id}").stdout.strip()
        base.tmux("link-window", "-d", "-s", window, "-t", "centrale_control_itest:")

        # Orphans the sweep must kill, and Codex's per-user daemon it must spare.
        base.wait_until(lambda: len(_pids_running(ORPHANS)) == 2, message="probe children")
        daemon_dir = os.path.join(self.worker_home, ".codex", "packages", "app-server-daemon")
        os.makedirs(daemon_dir)
        daemon_exe = os.path.join(daemon_dir, "centrale-itest-daemon")
        shutil.copy(shutil.which("sleep"), daemon_exe)
        daemon = subprocess.Popen([daemon_exe, "600"], cwd=self.wt, start_new_session=True)
        self.track_proc(daemon)

        status, body = remote.end_session(self.config, self.placed, self.task_id)
        self.assertEqual(status, 200, body)
        self.assertFalse(self.session_alive())
        windows = base.tmux("list-windows", "-a", "-F", "#{window_id}").stdout.split()
        self.assertNotIn(window, windows)
        base.wait_until(lambda: not _pids_running(PROBE_PROCESSES), message="nohup/setsid orphans swept")
        self.assertEqual(len(body["killedOrphans"]), 2)
        self.assertIsNone(daemon.poll(), "the Codex daemon stand-in must survive the sweep")

        # Resume reuses the worktree.
        result = spawn.resume(self.config, self.PROJECT, self.task_id)
        self.assertTrue(self.session_alive())
        self.assertTrue(result["resumed"])

        # Owed: the worker stops answering; End session is recorded, then retried.
        self.unreachable = True
        status, body = remote.end_session(self.config, self.placed, self.task_id)
        self.assertEqual(status, 202)
        self.assertTrue(self.session_alive())
        self.unreachable = False
        report = machines.refresh(self.config)
        remote.after_refresh(self.config, report)
        base.wait_until(lambda: not remote.owed_teardowns(), message="owed End session settled")
        self.assertFalse(self.session_alive())

        # The refresh fetched the task branch back under refs/remotes.
        ref = remote.tracking_ref("loop", self.task_id)
        self.assertEqual(base.run(["git", "rev-parse", ref], cwd=self.hub_repo).stdout.strip(), hub_main)
        self.assertEqual(remote.fetch_status(self.project, "loop")["fetchAgeSeconds"], 0)

        # Work on the worker, merged on the hub, task Done there: cleanup.
        with open(os.path.join(self.wt, "work.txt"), "w") as f:
            f.write("done\n")
        base.run(["git", "add", "work.txt"], cwd=self.wt)
        base.run(["git", "-c", "user.email=w@example.com", "-c", "user.name=w", "commit", "-q", "-m",
                  "work"], cwd=self.wt)
        remote.fetch_task_branches(self.config, self.machine, self.project)
        base.run(["git", "merge", "-q", "--no-ff", "-m", "merge", ref], cwd=self.hub_repo)
        base.run(["backlog", "task", "edit", self.task_id, "-s", "Done"], cwd=self.hub_repo)
        base.run(["git", "commit", "-q", "-am", "done"], cwd=self.hub_repo)
        with open(os.path.join(self.wt, "scratch.txt"), "w") as f:
            f.write("unsaved\n")

        with server.task_lifecycle_lock(self.PROJECT, self.task_id):
            status, body = remote.cleanup(self.config, self.placed, self.task_id)
        self.assertEqual(status, 409, body)
        self.assertEqual(body["discardedPaths"], ["scratch.txt"])
        self.assertTrue(os.path.isfile(os.path.join(self.wt, "scratch.txt")))
        with server.task_lifecycle_lock(self.PROJECT, self.task_id):
            status, body = remote.cleanup(self.config, self.placed, self.task_id, force=True)
        self.assertEqual(status, 200, body)
        self.assertEqual(body["discardedPaths"], ["scratch.txt"])
        self.assertFalse(os.path.exists(self.wt))
        self.assertNotEqual(base.run(["git", "--git-dir", bare, "rev-parse", "--verify", "-q",
                                      f"refs/heads/task/{self.task_id.lower()}"], check=False).returncode, 0)
        self.assertNotEqual(base.run(["git", "rev-parse", "--verify", "-q", ref], cwd=self.hub_repo,
                                     check=False).returncode, 0)
        self.assertIsNone(daemon.poll())

    def test_no_agent_ruling_commits_only_the_worker_task_file(self):
        spawn.spawn(self.config, self.PROJECT, self.task_id)
        base.run(["git", "config", "user.name", "Integration Test"], cwd=self.wt)
        base.run(["git", "config", "user.email", "integration@example.invalid"], cwd=self.wt)
        status, body = remote.end_session(self.config, self.placed, self.task_id)
        self.assertEqual(status, 200, body)
        with open(os.path.join(self.wt, "unrelated.txt"), "w") as f:
            f.write("leave staged\n")
        base.run(["git", "add", "unrelated.txt"], cwd=self.wt)
        before = base.run(["git", "rev-parse", "HEAD"], cwd=self.wt).stdout.strip()

        result = remote.commit_ruling(self.config, self.placed, self.task_id,
                                      "reviewer", "Keep the flag")

        self.assertEqual(result["branch"], f"task/{self.task_id.lower()}")
        self.assertNotEqual(result["commit"], before)
        self.assertEqual(result["commit"], base.run(["git", "rev-parse", "HEAD"], cwd=self.wt).stdout.strip())
        self.assertEqual(base.run(["git", "show", "--format=", "--name-only", "HEAD"],
                                  cwd=self.wt).stdout.strip(), result["path"])
        self.assertIn("unrelated.txt", base.run(["git", "diff", "--cached", "--name-only"],
                                                 cwd=self.wt).stdout.splitlines())
        self.assertIn("Keep the flag", base.run(["backlog", "task", "view", self.task_id, "--plain"],
                                                  cwd=self.wt).stdout)
        with open(os.path.join(self.wt, result["path"]), "a") as f:
            f.write("\nUncommitted note\n")
        with self.assertRaisesRegex(spawn.SpawnError, "uncommitted edits") as refused:
            remote.commit_ruling(self.config, self.placed, self.task_id, "reviewer", "Second ruling")
        self.assertEqual(refused.exception.status, 409)
        self.assertEqual(base.run(["git", "rev-parse", "HEAD"], cwd=self.wt).stdout.strip(), result["commit"])

    def test_worker_stand_in_pane_is_captured_over_control_connection(self):
        spawn.spawn(self.config, self.PROJECT, self.task_id)
        remote.end_session(self.config, self.placed, self.task_id)
        name = self.session()
        base.tmux("new-session", "-d", "-s", name, "-c", self.wt,
                  "sh", "-c", "printf 'worker stand-in pane\\n'; read reply; "
                  "printf 'worker got:%s\\n' \"$reply\"; sleep 30", check=True)
        self.addCleanup(base.kill_test_tmux_session, name)
        for args in remote_status.identity_commands(f"={name}:", self.PROJECT, self.task_id, "custom"):
            base.tmux(*args, check=True)

        controls = []

        def local_control(_machine):
            proc = subprocess.Popen(
                [shutil.which("tmux"), "-L", base.TMUX_SOCKET, "-C", "new-session", "-A",
                 "-s", remote_status.CONTROL_SESSION, "sleep infinity"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, bufsize=1)
            controls.append(proc)
            return proc

        def close_controls():
            for proc in controls:
                proc.terminate()
                proc.wait(timeout=5)
                proc.stdin.close()
                proc.stdout.close()

        self.addCleanup(close_controls)
        self.addCleanup(remote_status.stop_all)
        self.addCleanup(base.kill_test_tmux_session, remote_status.CONTROL_SESSION)
        self.addCleanup(server.remote_pane_capture.clear)
        self.addCleanup(server.remote_task_pane_capture.clear)
        with mock.patch.object(remote_status, "open_control", side_effect=local_control):
            report = remote_status.start(self.config, wait=5)
            self.assertEqual(report[0]["reach"], remote_status.REACHABLE, report)
            base.wait_until(lambda: ("loop", self.PROJECT, self.task_id.upper()) in server.remote_task_pane_capture,
                            message="worker control capture")
            lines = server.capture_task_pane(self.config, self.project, self.task_id, 40)
            server.send_task_input(self.config, self.project, self.task_id,
                                   name, "text", "hello worker")
            base.wait_until(
                lambda: "worker got:hello worker" in server.capture_task_pane(
                    self.config, self.project, self.task_id, 40),
                message="worker stand-in reply")
        self.assertIn("worker stand-in pane", lines)

    # -- task-208: merge gates, Discard and Abandon on the worker -------------

    def commit_on_worker(self, name, done=False):
        with open(os.path.join(self.wt, name), "w") as f:
            f.write("work\n")
        if done:
            base.run(["backlog", "task", "edit", self.task_id, "-s", "Done"], cwd=self.wt)
        base.run(["git", "add", "-A"], cwd=self.wt)
        base.run(["git", "-c", "user.email=w@example.com", "-c", "user.name=w", "commit", "-q", "-m",
                  name], cwd=self.wt)
        return base.run(["git", "rev-parse", "HEAD"], cwd=self.wt).stdout.strip()

    def worker_branch(self):
        bare = os.path.join(self.worker_home, "repos", self.PROJECT, ".git")
        proc = base.run(["git", "--git-dir", bare, "rev-parse", "--verify", "-q",
                         f"refs/heads/task/{self.task_id.lower()}"], check=False)
        return proc.stdout.strip() if proc.returncode == 0 else None

    def gate(self, report, name):
        return next(g for g in report["gates"] if g["name"] == name)

    def test_merge_gates_run_on_the_worker_and_a_timed_out_check_leaves_nothing(self):
        spawn.spawn(self.config, self.PROJECT, self.task_id)
        tip = self.commit_on_worker("work.txt", done=True)
        remote.fetch_task_branches(self.config, self.machine, self.project)
        self.assertEqual(harvest.task_branches(self.config, self.project), [f"task/{self.task_id.lower()}"])

        report = harvest.evaluate_branch(self.config, self.project, self.task_id)
        self.assertIn("live tmux session is still running on machine loop",
                      self.gate(report, "noLiveSession")["reason"])
        status, _ = remote.end_session(self.config, self.placed, self.task_id)
        self.assertEqual(status, 200)

        with open(os.path.join(self.wt, "notes.txt"), "w") as f:
            f.write("unsaved\n")
        report = harvest.evaluate_branch(self.config, self.project, self.task_id)
        self.assertIn("worktree on machine loop has uncommitted changes",
                      self.gate(report, "worktreeClean")["reason"])
        os.unlink(os.path.join(self.wt, "notes.txt"))

        # A hung checkCommand is killed ON THE WORKER, children included.
        self.project.update(checkCommand=HUNG_CHECK, checkTimeoutSeconds=2)
        report = harvest.evaluate_branch(self.config, self.project, self.task_id)
        self.assertTrue(self.gate(report, "mergeClean")["passed"], report)
        self.assertEqual(self.gate(report, "checkCommand")["reason"],
                         "checkCommand timed out after 2s on machine loop (killed there)")
        base.wait_until(lambda: not _pids_running(CHECK_PROCESSES), timeout=5,
                        message="the timed-out check and its children are gone from the worker")
        self.assertEqual(os.listdir(self.worker_tmp), [])
        self.assertEqual(self.worker_branch(), tip)

        # Green: merged on the hub at the pinned tip, cleaned up on the worker.
        self.project.update(checkCommand="test -f work.txt", checkTimeoutSeconds=30)
        report = harvest.harvest_branch(self.config, self.PROJECT, self.task_id)
        self.assertTrue(report["merged"], report)
        self.assertNotIn("warnings", report)
        self.assertEqual(base.run(["git", "merge-base", "--is-ancestor", tip, "main"], cwd=self.hub_repo,
                                  check=False).returncode, 0)
        bare = os.path.join(self.worker_home, "repos", self.PROJECT, ".git")
        hub_main = base.run(["git", "rev-parse", "main"], cwd=self.hub_repo).stdout.strip()
        self.assertEqual(base.run(["git", "--git-dir", bare, "rev-parse", "main"]).stdout.strip(), hub_main)
        self.assertIsNone(self.worker_branch())
        self.assertFalse(os.path.exists(self.wt))
        self.assertEqual(harvest.task_branches(self.config, self.project), [])

    def test_a_base_that_moved_on_the_worker_refuses_the_merge_test(self):
        spawn.spawn(self.config, self.PROJECT, self.task_id)
        self.commit_on_worker("work.txt", done=True)
        remote.end_session(self.config, self.placed, self.task_id)
        machine = self.machine
        tip = self.worker_branch()
        hub_main = base.run(["git", "rev-parse", "main"], cwd=self.hub_repo).stdout.strip()
        ok, reason, _, _ = remote_harvest.scratch_merge_and_check(
            self.config, machine, self.placed, self.task_id, "main", "f" * 40, tip)
        self.assertFalse(ok)
        self.assertIn("the base moved: main on machine loop is at " + hub_main[:10], reason)
        ok, reason, _, _ = remote_harvest.scratch_merge_and_check(
            self.config, machine, self.placed, self.task_id, "main", hub_main, "f" * 40)
        self.assertFalse(ok)
        self.assertIn("moved on machine loop since it was pinned", reason)
        self.assertEqual(os.listdir(self.worker_tmp), [])

    def test_abandon_then_discard_on_the_worker(self):
        spawn.spawn(self.config, self.PROJECT, self.task_id)
        tip = self.commit_on_worker("work.txt")
        remote.end_session(self.config, self.placed, self.task_id)
        with open(os.path.join(self.wt, "notes.txt"), "w") as f:
            f.write("unsaved\n")

        status, preview = remote_harvest.discard_preview(self.config, self.placed, self.task_id)
        self.assertEqual(status, 200, preview)
        self.assertEqual((preview["branchTip"], preview["commitCount"], preview["dirtyPaths"]),
                         (tip, 1, ["notes.txt"]))
        with server.task_lifecycle_lock(self.PROJECT, self.task_id):
            status, body = remote_harvest.throw_away(self.config, self.placed, self.task_id,
                                                     tip, [], "abandon")
        self.assertEqual(status, 409, body)  # the confirm named no uncommitted file
        self.assertTrue(os.path.exists(os.path.join(self.wt, "notes.txt")))
        with server.task_lifecycle_lock(self.PROJECT, self.task_id):
            status, body = remote_harvest.throw_away(self.config, self.placed, self.task_id,
                                                     tip, ["notes.txt"], "abandon")
        self.assertEqual(status, 200, body)
        self.assertFalse(os.path.exists(self.wt))
        self.assertEqual(self.worker_branch(), tip)

        with server.task_lifecycle_lock(self.PROJECT, self.task_id):
            status, body = remote_harvest.throw_away(self.config, self.placed, self.task_id,
                                                     tip, [], "discard")
        self.assertEqual(status, 200, body)
        self.assertIsNone(self.worker_branch())
        tagged = base.run(["git", "rev-parse", body["recoveryTag"]], cwd=self.hub_repo).stdout.strip()
        self.assertEqual(tagged, tip)
        self.assertNotEqual(base.run(["git", "rev-parse", "--verify", "-q", remote.tracking_ref("loop", self.task_id)],
                                     cwd=self.hub_repo, check=False).returncode, 0)

    # -- task-224: one project, a task on the hub and one on the worker ------

    def test_one_project_with_a_task_on_the_hub_and_one_on_the_worker_merges_both(self):
        hub_task = self.task_id
        worker_task = base.create_task(self.hub_repo, "Second probe task", status="To Do")
        base.run(["git", "add", "-A"], cwd=self.hub_repo)
        base.run(["git", "commit", "-q", "-m", "second task"], cwd=self.hub_repo)

        hub_result = spawn.spawn(self.config, self.PROJECT, hub_task, machine="hub")
        self.assertNotIn("machine", hub_result)
        worker_result = spawn.spawn(self.config, self.PROJECT, worker_task)  # defaultMachine
        self.assertEqual(worker_result["machine"], "loop")

        # Where each is, derived fresh from git -- and a spawn elsewhere refused.
        hub_location = machines.locate(self.config, self.project, hub_task)
        self.assertEqual(hub_location.as_json()["machine"], "hub")
        self.assertEqual(machines.locate(self.config, self.project, worker_task).as_json(),
                         {"machine": "loop", "conflict": None, "reach": "reachable"})
        with self.assertRaises(spawn.SpawnError) as caught:
            spawn.spawn(self.config, self.PROJECT, hub_task, machine="loop")
        self.assertEqual(caught.exception.status, 409)
        self.assertIn("already on the hub", caught.exception.message)
        board = server.get_board(self.config)
        tasks = {t["id"]: t for t in board["projects"][0]["tasks"]}
        self.assertEqual(tasks[hub_task]["location"]["machine"], "hub")
        self.assertNotIn("machine", tasks[hub_task])
        self.assertEqual((tasks[worker_task]["location"]["machine"], tasks[worker_task]["machine"]),
                         ("loop", "loop"))
        self.assertEqual(tasks[worker_task]["branchCheckout"]["kind"], "centrale")

        # Work and Done on each task's own branch, where it is.
        hub_wt = spawn.worktree_dir(self.config, self.PROJECT, hub_task)
        worker_wt = os.path.join(self.worker_home, "work", f"{self.PROJECT}-{worker_task.lower()}")
        for wt, task, name in ((hub_wt, hub_task, "hub-work.txt"), (worker_wt, worker_task, "worker-work.txt")):
            with open(os.path.join(wt, name), "w") as f:
                f.write("work\n")
            base.run(["backlog", "task", "edit", task, "-s", "Done"], cwd=wt)
            base.run(["git", "add", "-A"], cwd=wt)
            base.run(["git", "-c", "user.email=w@example.com", "-c", "user.name=w", "commit", "-q", "-m",
                      name], cwd=wt)

        # End both sessions and merge both, through the same calls.
        base.tmux("kill-session", "-t", f"={spawn.session_name(self.PROJECT, hub_task)}")
        server.kill_pids_with_cwd_under(hub_wt)
        status, body = remote.end_session(self.config, self.placed, worker_task)
        self.assertEqual(status, 200, body)
        for task in (hub_task, worker_task):
            report = harvest.harvest_branch(self.config, self.PROJECT, task)
            self.assertTrue(report["merged"], report)
        self.assertEqual(report["machine"], "loop")
        files = base.run(["git", "ls-tree", "--name-only", "main"], cwd=self.hub_repo).stdout.split()
        self.assertIn("hub-work.txt", files)
        self.assertIn("worker-work.txt", files)
        self.assertEqual(harvest.task_branches(self.config, self.project), [])
        self.assertFalse(os.path.exists(worker_wt))

    def test_interrupted_launch_is_reported_unknown_and_a_retry_refuses(self):
        real = self.loopback_ssh

        def drop_after_launch(argv, input=None, timeout=None):
            rc, out, err, timed_out = real(argv, input, timeout)
            if " new-session " in argv[-1]:
                return rc, out.split(" 0 rc ")[0], "", True  # started, answer lost
            return rc, out, err, timed_out

        with mock.patch.object(machines, "run_ssh", drop_after_launch):
            with self.assertRaises(spawn.SpawnError) as caught:
                spawn.spawn(self.config, self.PROJECT, self.task_id)
        self.assertEqual(caught.exception.status, 504)
        self.assertTrue(self.session_alive())  # it did start
        with self.assertRaises(spawn.SpawnError) as caught:
            spawn.spawn(self.config, self.PROJECT, self.task_id)
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(len(base.tmux("list-sessions", "-F", "#{session_name}").stdout.split()), 1)
        status, _ = remote.end_session(self.config, self.placed, self.task_id)
        self.assertEqual(status, 200)


@unittest.skipUnless(os.environ.get(SSH_TARGET_ENV), f"set {SSH_TARGET_ENV} to an ssh target to run")
@base.require_tools("git", "backlog", "tmux")
class SshTargetTests(base.IntegrationCase):
    """The same flow against a real sshd. The worker needs git, tmux >= 3.2
    and python3; nothing is installed there and everything this creates is
    under one directory it removes afterwards."""

    PROJECT = "remoteproj"

    def setUp(self):
        super().setUp()
        self.target = os.environ[SSH_TARGET_ENV]
        self.root = os.environ.get(SSH_ROOT_ENV) or f"~/centrale-itest-{os.getpid()}"
        self.socket = f"centrale-itest-{os.getpid()}-{secrets.token_hex(3)}"
        ssh_dir = os.path.join("/tmp", f"c207s-{secrets.token_hex(3)}")
        patcher = mock.patch.dict(os.environ, {
            machines.SSH_DIR_ENV: ssh_dir, "CENTRALE_SPAWN_CMD": PROBE,
            remote.OWED_ENV: os.path.join(self.tmp_dir, "owed.json")})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.machine = {"name": "itest", "ssh": self.target, "repoRoot": f"{self.root}/repos",
                        "worktreeRoot": f"{self.root}/work", "maxAgents": None, "tmuxSocket": self.socket}
        self.hub_repo = base.init_backlog_repo(self.tmp_dir, name=self.PROJECT)
        self.task_id = base.create_task(self.hub_repo, "Remote probe task", status="To Do")
        base.run(["git", "add", "-A"], cwd=self.hub_repo)
        base.run(["git", "commit", "-q", "-m", "task"], cwd=self.hub_repo)
        self.config = {"machines": [self.machine],
                       "projects": [{"name": self.PROJECT, "path": self.hub_repo, "machines": ["itest"],
                                     "defaultMachine": "itest", "maxAgents": None, "worktreeLinks": []}],
                       "worktreeRoot": os.path.join(self.tmp_dir, "hub-worktrees"),
                       "lockSpawnedTaskFiles": False, "refreshIntervalSeconds": 10}
        self.project = self.config["projects"][0]
        self.addCleanup(self.cleanup_worker, ssh_dir)
        setup = machines.run_on_machine(self.config, "itest", ["sh", "-c",
                                        'mkdir -p "$1" && git init -q -b parking "$1/remoteproj" && '
                                        'git -C "$1/remoteproj" -c user.name=Probe '
                                        '-c user.email=probe@example.invalid commit -qm parked --allow-empty && '
                                        'git -C "$1/remoteproj" checkout -q --detach HEAD',
                                        "sh", machines.MachinePath(f"{self.root}/repos")])
        self.assertTrue(setup.ok, setup)

    def cleanup_worker(self, ssh_dir):
        machines.run_on_machine(self.config, "itest", ["sh", "-c",
                                f'tmux -L {self.socket} kill-server 2>/dev/null; rm -rf "$1"; '
                                f'rm -f "${{TMUX_TMPDIR:-/tmp}}/tmux-$(id -u)/{self.socket}"; '
                                'pkill -fx "sleep 30[789]" || true', "sh", machines.MachinePath(self.root)])
        subprocess.run(["ssh", "-F", os.path.join(ssh_dir, "config"), "-O", "exit", self.target],
                       capture_output=True)
        shutil.rmtree(ssh_dir, ignore_errors=True)

    @property
    def placed(self):
        return machines.placed(self.project, "itest")

    def worker(self, script):
        return machines.run_on_machine(self.config, "itest", ["sh", "-c", script])

    def test_spawn_end_session_resume_cleanup_over_ssh(self):
        result = spawn.spawn(self.config, self.PROJECT, self.task_id)
        name = result["session"]
        tmux = f"tmux -L {self.socket}"
        self.assertTrue(self.worker(f"{tmux} has-session -t ={name}").ok)
        time.sleep(1)
        self.assertEqual(self.worker("pgrep -fxc 'sleep 30[78]'").stdout.strip(), "2")
        status, body = remote.end_session(self.config, self.placed, self.task_id)
        self.assertEqual(status, 200, body)
        self.assertFalse(self.worker(f"{tmux} has-session -t ={name}").ok)
        time.sleep(1)
        self.assertNotEqual(self.worker("pgrep -fx 'sleep 30[789]'").returncode, 0)
        spawn.resume(self.config, self.PROJECT, self.task_id)
        self.assertTrue(self.worker(f"{tmux} has-session -t ={name}").ok)
        status, body = remote.end_session(self.config, self.placed, self.task_id)
        self.assertEqual(status, 200, body)
        remote.after_refresh(self.config, machines.refresh(self.config))
        ref = remote.tracking_ref("itest", self.task_id)
        self.assertEqual(base.run(["git", "rev-parse", "--verify", ref], cwd=self.hub_repo).returncode, 0)
        base.run(["git", "merge", "-q", "--no-ff", "-m", "merge", ref], cwd=self.hub_repo)
        base.run(["backlog", "task", "edit", self.task_id, "-s", "Done"], cwd=self.hub_repo)
        base.run(["git", "commit", "-q", "-am", "done"], cwd=self.hub_repo)
        with server.task_lifecycle_lock(self.PROJECT, self.task_id):
            status, body = remote.cleanup(self.config, self.placed, self.task_id)
        self.assertEqual(status, 200, body)
        self.assertNotEqual(self.worker(f'test -d "$HOME/{self.root[2:]}/work/{self.PROJECT}-'
                                        f'{self.task_id.lower()}"').returncode, 0)


    def test_one_task_on_the_hub_and_one_on_the_target_merge_over_ssh(self):
        """task-224 against a real worker: the same project, one task spawned
        here and one there, both merged into the hub's main."""
        hub_task = self.task_id
        worker_task = base.create_task(self.hub_repo, "Second probe task", status="To Do")
        base.run(["git", "add", "-A"], cwd=self.hub_repo)
        base.run(["git", "commit", "-q", "-m", "second task"], cwd=self.hub_repo)
        hub_result = spawn.spawn(self.config, self.PROJECT, hub_task, machine="hub")
        self.assertNotIn("machine", hub_result)
        self.addCleanup(base.kill_test_tmux_session, spawn.session_name(self.PROJECT, hub_task))
        worker_result = spawn.spawn(self.config, self.PROJECT, worker_task, machine="itest")
        self.assertEqual(worker_result["machine"], "itest")
        self.assertEqual(machines.locate(self.config, self.project, hub_task).as_json()["machine"], "hub")
        self.assertEqual(machines.locate(self.config, self.project, worker_task).as_json(),
                         {"machine": "itest", "conflict": None, "reach": "reachable"})

        hub_wt = spawn.worktree_dir(self.config, self.PROJECT, hub_task)
        with open(os.path.join(hub_wt, "hub-work.txt"), "w") as f:
            f.write("work\n")
        base.run(["backlog", "task", "edit", hub_task, "-s", "Done"], cwd=hub_wt)
        base.run(["git", "add", "-A"], cwd=hub_wt)
        base.run(["git", "-c", "user.email=w@example.com", "-c", "user.name=w", "commit", "-q", "-m",
                  "hub work"], cwd=hub_wt)
        # On the worker, without its backlog CLI (often not on ssh's PATH):
        # the task file's status line, and a work file, committed there.
        worker_wt = machines.MachinePath(f"{self.root}/work/{self.PROJECT}-{worker_task.lower()}")
        committed = machines.run_on_machine(self.config, "itest", ["sh", "-c", (
            'cd "$1" && for f in backlog/tasks/"$2"\\ -*; do sed -i "s/^status: .*/status: Done/" "$f"; done '
            '&& echo work > worker-work.txt && git add -A '
            '&& git -c user.email=w@example.com -c user.name=w commit -q -m "worker work"'),
            "sh", worker_wt, worker_task.lower()])
        self.assertTrue(committed.ok, committed)

        base.tmux("kill-session", "-t", f"={spawn.session_name(self.PROJECT, hub_task)}")
        server.kill_pids_with_cwd_under(hub_wt)
        status, body = remote.end_session(self.config, self.placed, worker_task)
        self.assertEqual(status, 200, body)
        for task in (hub_task, worker_task):
            report = harvest.harvest_branch(self.config, self.PROJECT, task)
            self.assertTrue(report["merged"], report)
            self.assertNotIn("warnings", report)
        files = base.run(["git", "ls-tree", "--name-only", "main"], cwd=self.hub_repo).stdout.split()
        self.assertIn("hub-work.txt", files)
        self.assertIn("worker-work.txt", files)
        self.assertEqual(harvest.task_branches(self.config, self.project), [])
        self.assertNotEqual(self.worker(f'test -d "$HOME/{self.root[2:]}/work/{self.PROJECT}-'
                                        f'{worker_task.lower()}"').returncode, 0)

if __name__ == "__main__":
    unittest.main()


@base.require_tools('git', 'python3', 'timeout', 'setsid')
class WorkerReadinessScratchTests(base.IntegrationCase):
    """task-240: real scratch git/timeout locally; no ssh or agents."""
    def setUp(self):
        super().setUp()
        from pathlib import Path
        self.root = Path(self.tmp_dir)
        self.repo = self.root / 'repos' / 'app'
        self.repo.mkdir(parents=True)
        self.env = dict(os.environ, HOME=self.tmp_dir, TMPDIR=self.tmp_dir,
                        GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull)
        self.git('init', '-b', 'main')
        self.git('config', 'user.name', 'alex')
        self.git('config', 'user.email', 'alex@example.test')
        (self.repo / 'check.sh').write_text('#!/bin/sh\ntest -L .venv && cat .venv/ready\n')
        (self.repo / 'check.sh').chmod(0o755)
        self.git('add', 'check.sh')
        self.git('commit', '-m', 'read-only check')
        self.sha = self.git('rev-parse', 'HEAD').stdout.strip()
        self.git('checkout', '--detach')
        (self.repo / '.venv').mkdir()
        (self.repo / '.venv' / 'ready').write_text('ready')
        self.machine = {'name': 'box-b', 'repoRoot': str(self.root / 'repos'), 'worktreeRoot': str(self.root / 'work')}
        self.project = {'name': 'app', 'path': str(self.repo), 'worktreeLinks': ['.venv'],
                        'workerCheck': {'command': 'check.sh', 'doc': 'docs/setup.md'}}

    def git(self, *args):
        return subprocess.run(['git', '-C', str(self.repo), *args], env=self.env, text=True,
                              capture_output=True, check=True)

    def local_worker(self, config, name, argv, **kwargs):
        proc = subprocess.run([str(a) for a in argv], env=self.env, text=True, capture_output=True,
                              timeout=kwargs.get('timeout', 20))
        return machines.MachineResult(machines.ANSWERED, proc.returncode, proc.stdout, proc.stderr, name)

    def assert_clean(self):
        self.assertEqual(len(self.git('worktree', 'list', '--porcelain').stdout.split('worktree ')) - 1, 1)
        self.assertEqual(self.git('for-each-ref', '--format=%(refname:short)', 'refs/heads/').stdout.strip(), 'main')
        self.assertEqual(list(self.root.glob('centrale-worker-check-*')), [])
        self.assertEqual((self.repo / '.venv' / 'ready').read_text(), 'ready')

    def test_exact_pinned_tree_links_and_cleanup_on_pass(self):
        # A later main cannot replace the commit passed to the check.
        self.git('checkout', 'main')
        (self.repo / 'check.sh').write_text('#!/bin/sh\nexit 1\n')
        self.git('commit', '-am', 'later failing check')
        self.git('checkout', '--detach')
        with mock.patch.object(machines, 'run_on_machine', self.local_worker):
            passed, reason = remote.check_worker_readiness({}, self.machine, self.project, self.sha)
        self.assertTrue(passed, reason)
        self.assert_clean()

    def test_failure_missing_command_and_timeout_cleanup(self):
        for command, text in (('check.sh missing', 'passed'), ('absent.sh', 'setup not finished')):
            self.project['workerCheck']['command'] = command
            with mock.patch.object(machines, 'run_on_machine', self.local_worker):
                passed, reason = remote.check_worker_readiness({}, self.machine, self.project, self.sha)
            self.assertEqual(passed, text == 'passed', reason)
            self.assertIn(text, reason)
            self.assert_clean()
        # New committed checks exercise failure and TERM/KILL timeout.
        for body, expected in (('echo missing-venv; exit 1', 'failed'), ('sleep 10', 'timed out')):
            self.git('checkout', 'main')
            (self.repo / 'check.sh').write_text('#!/bin/sh\n' + body + '\n')
            self.git('commit', '-am', expected)
            sha = self.git('rev-parse', 'HEAD').stdout.strip()
            self.git('checkout', '--detach')
            self.project['workerCheck']['command'] = 'check.sh'
            with mock.patch.object(machines, 'run_on_machine', self.local_worker), mock.patch.object(remote, 'WORKER_CHECK_SECONDS', 0.2):
                passed, reason = remote.check_worker_readiness({}, self.machine, self.project, sha)
            self.assertFalse(passed)
            self.assertIn(expected, reason)
            self.assert_clean()


    def test_term_ignoring_background_descendants_are_removed(self):
        pidfile = self.root / 'escaped-pids'
        self.git('checkout', 'main')
        body = '''#!/bin/sh
setsid sh -c 'trap "" TERM; sleep 60 & child=$!; printf "%s %s" "$$" "$child" > "$1"; wait' sh "$1" >/dev/null 2>&1 &
while [ ! -s "$1" ]; do sleep 0.02; done
'''
        (self.repo / 'check.sh').write_text(body)
        self.git('commit', '-am', 'background descendant check')
        sha = self.git('rev-parse', 'HEAD').stdout.strip()
        self.git('checkout', '--detach')
        self.project['workerCheck']['command'] = 'check.sh ' + str(pidfile)
        def remove_leftovers():
            import signal
            if pidfile.exists():
                for pid in pidfile.read_text().split():
                    try:
                        os.kill(int(pid), signal.SIGKILL)
                    except ProcessLookupError:
                        pass
        self.addCleanup(remove_leftovers)
        with mock.patch.object(machines, 'run_on_machine', self.local_worker):
            passed, reason = remote.check_worker_readiness({}, self.machine, self.project, sha)
        self.assertTrue(passed, reason)
        for pid in pidfile.read_text().split():
            self.assertFalse(os.path.exists('/proc/' + pid + '/cwd'), 'escaped process still alive: ' + pid)
        self.assert_clean()


    def test_present_check_exit_126_or_127_is_an_ordinary_failure(self):
        for rc in (126, 127):
            self.git('checkout', 'main')
            (self.repo / 'check.sh').write_text('#!/bin/sh\necho missing dependency; exit ' + str(rc) + '\n')
            self.git('commit', '-am', 'dependency missing ' + str(rc))
            sha = self.git('rev-parse', 'HEAD').stdout.strip()
            self.git('checkout', '--detach')
            with mock.patch.object(machines, 'run_on_machine', self.local_worker):
                passed, reason = remote.check_worker_readiness({}, self.machine, self.project, sha)
            self.assertFalse(passed)
            self.assertIn('failed (exit ' + str(rc) + ')', reason)
            self.assertIn('missing dependency', reason)
            self.assertNotIn('setup not finished', reason)
            self.assert_clean()

    def test_no_proc_still_removes_scratch_tree(self):
        sweep = "from unittest import mock\nwith mock.patch('os.path.isdir', return_value=False), mock.patch('os.listdir', side_effect=AssertionError('no proc')):\n    exec(" + repr(remote._WORKER_SWEEP_PROGRAM) + ")\n"
        with mock.patch.object(machines, 'run_on_machine', self.local_worker), \
             mock.patch.object(remote, '_WORKER_SWEEP_PROGRAM', sweep):
            passed, reason = remote.check_worker_readiness({}, self.machine, self.project, self.sha)
        self.assertTrue(passed, reason)
        self.assert_clean()
