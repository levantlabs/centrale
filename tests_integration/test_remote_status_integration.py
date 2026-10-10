"""task-209: agent status from a worker, end to end.

A real, unmodified server.py (deployed with base.deploy_app) on an
ephemeral port, configured with a machine whose task panes live on a
SEPARATE tmux server (its own `-L` socket, `tmuxSocket`). The worker's
panes get their identity options the way a spawn sets them
(remote_status.identity_commands) and their state from the real generated
hook command (server.hook_argv's "tmux" sink), run inside the pane's
environment. The server reads them over a real `tmux -C` control
connection, and the test checks what an orchestrator hears: the read-back
states at boot before anything else, each later change, a new task session
armed without being asked, and the same again after a restart.

Two variants of the same flow:

- `LocalWorkerTests` always runs: `ssh` is a stand-in on the server's PATH
  that runs the remote command with a local `sh`, so everything but the
  network is real (control mode, link-window, subscriptions, the hook).
  A second machine whose stand-in exits 255 is the unreachable worker.
- `RealSshWorkerTests` runs the same flow against a real ssh target, and
  only when CENTRALE_TEST_SSH_TARGET names one (an ssh target the hub can
  reach with BatchMode, tmux >= 3.2 and python3 or jq on it). Unset, it
  skips: the hub has no sshd of its own to test against. Everything it
  creates on the target -- one tmux server on its own socket -- is
  removed afterwards.

Run explicitly: python3 -m unittest tests_integration.test_remote_status_integration
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request

from tests_integration import base

import remote_status
import server

PROJECT = "remoteproj"
FAR_PROJECT = "farproj"
SSH_TARGET_ENV = "CENTRALE_TEST_SSH_TARGET"
WORKER_SOCKET = base.TMUX_SOCKET + "-w"


def tearDownModule():
    base.assert_test_tmux_footprint_gone()
    base.assert_test_tmux_footprint_gone(WORKER_SOCKET)


class _WorkerFlow:
    """The flow both variants run; a subclass says how to reach the worker."""

    ssh_target = None  # what projects.json's machine "ssh" says

    def worker(self, script, check=True):
        """Run a POSIX sh script on the worker; returns stdout."""
        raise NotImplementedError

    def server_env(self):
        return {}

    def setUp(self):
        self.real_path = os.environ["PATH"]  # before the tmux shim goes in front
        super().setUp()
        self.repo_path = base.init_backlog_repo(self.tmp_dir, name=PROJECT)
        self.far_path = base.init_backlog_repo(self.tmp_dir, name=FAR_PROJECT)
        self.app_dir = base.deploy_app(os.path.join(self.tmp_dir, "app"))
        self.state_home = os.path.join(self.tmp_dir, "state")
        # ssh refuses a long ControlPath: a short directory of our own.
        self.ssh_dir = tempfile.mkdtemp(prefix="c209-", dir="/tmp")
        self.addCleanup(shutil.rmtree, self.ssh_dir, ignore_errors=True)
        self.addCleanup(self.close_ssh_master)
        self.port = base.free_port()
        base.write_projects_json(self.app_dir, {
            "port": self.port,
            "worktreeRoot": os.path.join(self.tmp_dir, "worktrees"),
            "machines": [
                {"name": "box-b", "ssh": self.ssh_target, "repoRoot": "~/c209-repos",
                 "worktreeRoot": "~/c209-work", "maxAgents": 2, "tmuxSocket": WORKER_SOCKET},
                {"name": "box-c", "ssh": "unreachable", "repoRoot": "~/r", "worktreeRoot": "~/w",
                 "maxAgents": 2},
            ],
            "projects": [{"name": PROJECT, "path": self.repo_path, "machines": ["box-b"],
                          "defaultMachine": "box-b"},
                         {"name": FAR_PROJECT, "path": self.far_path, "machines": ["box-c"],
                          "defaultMachine": "box-c"}],
        })
        self.addCleanup(self.worker, f"tmux -L {WORKER_SOCKET} kill-server 2>/dev/null; "
                                     f'rm -f "${{TMUX_TMPDIR:-/tmp}}/tmux-$(id -u)/{WORKER_SOCKET}"',
                        False)
        # The journal the far project's announcement is drawn from: a task
        # the previous process last saw live -- on box-c, as the hub's
        # fetched copy of its branch says (task-224: a task's location is
        # derived, and box-c cannot be asked).
        base.run(["git", "update-ref", "refs/remotes/box-c/task/task-3", "HEAD"], cwd=self.far_path)
        os.makedirs(os.path.join(self.state_home, "centrale"))
        with open(os.path.join(self.state_home, "centrale", "fleet.jsonl"), "w") as f:
            f.write(json.dumps({"project": FAR_PROJECT, "taskId": "TASK-3", "agent": "claude",
                                "state": "working", "timestamp": time.time() - 60}) + "\n")

    def close_ssh_master(self):
        config = os.path.join(self.ssh_dir, "config")
        if self.ssh_target != "worker" and os.path.exists(config):
            subprocess.run(["ssh", "-F", config, "-O", "exit", self.ssh_target],
                           capture_output=True, timeout=10)

    # -- the server ------------------------------------------------------

    def url(self, path, **query):
        return f"http://127.0.0.1:{self.port}{path}?" + urllib.parse.urlencode(query)

    def start_server(self):
        env = base.sandboxed_env(self.shim_dir, cache_home=self.cache_home, extra={
            "XDG_STATE_HOME": self.state_home, "CENTRALE_SSH_DIR": self.ssh_dir,
            **self.server_env()})
        proc = subprocess.Popen([sys.executable, "server.py"], cwd=self.app_dir, env=env,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                start_new_session=True)
        self.track_proc(proc)
        base.wait_until_http_ok(self.url("/api/orchestrator-wait", project=PROJECT, timeout=0),
                                timeout=30.0)
        return proc

    def stop_server(self, proc):
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=10)

    def lines(self, project, until=None, timeout=15.0):
        """Every line a fresh cursor hears, until `until` is among them."""
        cursor, lines = None, []
        deadline = time.monotonic() + timeout
        while True:
            query = {"project": project, "timeout": 1 if until else 0}
            if cursor:
                query["after"] = cursor
            with urllib.request.urlopen(self.url("/api/orchestrator-wait", **query), timeout=10) as r:
                position, message = r.read().decode().rstrip("\n").split(" ", 1)
            if message == "nothing yet":
                if until is None or until in lines or time.monotonic() > deadline:
                    return lines
            else:
                lines.append(message)
            cursor = position

    # -- the worker ------------------------------------------------------

    def tmux(self, *args):
        return f"tmux -L {WORKER_SOCKET} " + shlex.join(args)

    def new_task_session(self, task, kind="claude"):
        """A task session with its identity options, set in the same tmux
        call as new-session, as a spawn on a worker sets them."""
        name = f"centrale-{PROJECT}-{task.lower()}"
        argv = ["new-session", "-d", "-s", name, "-x", "120", "-y", "30", "sleep 600"]
        for command in remote_status.identity_commands(f"={name}:", PROJECT, task, kind):
            argv += [";", *command]
        self.worker(self.tmux(*argv) + " && " + self.tmux("display-message", "-p", "-t",
                                                         f"={name}:", "#{pane_id}"))
        return name

    def hook(self, session, state, stdin=""):
        """The generated hook command, run with the pane's own environment."""
        command = shlex.join(server.hook_argv(state, "tmux"))
        script = (f'pane=$({self.tmux("display-message", "-p", "-t", f"={session}:", "#{pane_id}")}); '
                  f'sock=$({self.tmux("display-message", "-p", "-t", f"={session}:", "#{socket_path}")}); '
                  f'printf %s {shlex.quote(stdin)} | TMUX="$sock,1,0" TMUX_PANE="$pane" {command}')
        self.worker(script)

    # -- the flow ---------------------------------------------------------

    def test_status_reaches_the_hub_and_survives_a_restart(self):
        # Start the worker's tmux server with the first task's session.
        task1 = self.new_task_session("TASK-1")
        self.hook(task1, "working")

        first = self.start_server()
        # Boot read the worker before announcing: the first lines are the
        # read-back state and the unreachable worker, nothing before them.
        self.assertEqual(self.lines(PROJECT),
                         ["TASK-1 working (read back from box-b after the server restart)"])
        far = self.lines(FAR_PROJECT)
        self.assertEqual(len(far), 1, far)
        self.assertTrue(far[0].startswith("TASK-3 box-c unreachable after the server restart; "
                                          "state unknown"), far)

        # A change on the worker reaches the hub through record_agent_event.
        self.hook(task1, "notification", json.dumps({"notification_type": "idle_prompt"}))
        self.hook(task1, "finished")
        self.assertIn("TASK-1 finished (ready to review)",
                      self.lines(PROJECT, until="TASK-1 finished (ready to review)"))

        # A new task session is linked and subscribed without being asked.
        task2 = self.new_task_session("TASK-2")
        time.sleep(1.5)  # the %sessions-changed scan runs; then its hook fires
        self.hook(task2, "notification", json.dumps({"notification_type": "permission_prompt"}))
        lines = self.lines(PROJECT, until="TASK-2 waiting for input")
        self.assertIn("TASK-2 waiting for input", lines)
        self.assertNotIn("TASK-1 working", " ".join(lines[1:]))  # no idle_prompt leak

        # Both task windows are linked into the control session, once each.
        windows = self.worker(self.tmux("list-windows", "-t", f"={remote_status.CONTROL_SESSION}",
                                        "-F", "#{window_id} #{window_linked}")).split("\n")
        self.assertEqual(sum(w.endswith(" 1") for w in windows if w), 2, windows)

        # Restart: read back again, both states, before anything else.
        self.stop_server(first)
        self.start_server()
        self.assertEqual(sorted(self.lines(PROJECT)), [
            "TASK-1 finished (ready to review) (read back from box-b after the server restart)",
            "TASK-2 waiting for input (read back from box-b after the server restart)",
        ])
        with urllib.request.urlopen(self.url("/api/fleet"), timeout=15) as response:
            fleet = json.loads(response.read())
        self.assertIn("machines", fleet)


@base.require_tools("git", "tmux", "backlog")
class LocalWorkerTests(_WorkerFlow, base.IntegrationCase):
    ssh_target = "worker"

    def setUp(self):
        # The ssh stand-in: drops ssh's options, refuses "unreachable" the
        # way ssh does (exit 255), and runs anything else here with the
        # real PATH -- never this run's tmux shim, so `tmux -L` means what
        # it says, as it would on a real worker.
        self.ssh_bin = tempfile.mkdtemp(prefix="centrale-itest-ssh-")
        self.addCleanup(shutil.rmtree, self.ssh_bin, ignore_errors=True)
        with open(os.path.join(self.ssh_bin, "ssh"), "w") as f:
            f.write("#!/bin/sh\n"
                    'while [ $# -gt 0 ]; do case $1 in -F|-o|-O) shift 2 ;; -*) shift ;; *) break ;; esac; done\n'
                    'target=$1; shift\n'
                    'if [ "$target" = unreachable ]; then\n'
                    '  echo "ssh: connect to host unreachable port 22: Connection refused" >&2; exit 255\n'
                    'fi\n'
                    f'PATH={shlex.quote(os.environ["PATH"])}; export PATH\n'
                    'exec sh -c "$*"\n')
        os.chmod(os.path.join(self.ssh_bin, "ssh"), 0o755)
        super().setUp()

    def server_env(self):
        return {"PATH": os.pathsep.join([self.ssh_bin, self.shim_dir, self.real_path])}

    def worker(self, script, check=True):
        env = dict(os.environ)
        env["PATH"] = self.real_path  # without this run's tmux shim
        proc = subprocess.run(["sh", "-c", script], env=env, capture_output=True, text=True,
                              timeout=30)
        if check and proc.returncode != 0:
            raise AssertionError(f"worker script failed: {proc.stderr}")
        return proc.stdout.strip()


@unittest.skipUnless(os.environ.get(SSH_TARGET_ENV), f"set {SSH_TARGET_ENV} to an ssh target")
@base.require_tools("git", "tmux", "backlog")  # and ssh: implied by naming a target
class RealSshWorkerTests(_WorkerFlow, base.IntegrationCase):
    ssh_target = os.environ.get(SSH_TARGET_ENV, "")

    # Everything this test may leave on the target lives under here.
    WORKER_DIR = "~/centrale-test-task-209"

    def setUp(self):
        super().setUp()
        config = self.read_config()
        config["machines"][0].update(repoRoot=f"{self.WORKER_DIR}/repos",
                                     worktreeRoot=f"{self.WORKER_DIR}/work")
        config["machines"][1]["ssh"] = "centrale-itest-unreachable.invalid"
        base.write_projects_json(self.app_dir, config)
        self.addCleanup(self.worker, f"rm -rf {self.WORKER_DIR}", False)

    def read_config(self):
        with open(os.path.join(self.app_dir, "projects.json")) as f:
            return json.load(f)

    def worker(self, script, check=True):
        proc = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
                               self.ssh_target, f"sh -c {shlex.quote(script)}"],
                              capture_output=True, text=True, timeout=60)
        if check and proc.returncode != 0:
            raise AssertionError(f"worker script failed ({proc.returncode}): {proc.stderr}")
        return proc.stdout.strip()


if __name__ == "__main__":
    unittest.main()
