"""task-201: what a real server restart keeps, end to end.

A real, unmodified server.py (deployed with base.deploy_app) on an
ephemeral port, with its fleet journal and delivery log under a private
XDG_STATE_HOME and its tmux on this run's `-L` socket. Sessions are plain
`sleep` panes; hook events come from the real centrale_notify.py, run the
way an agent's hook runs it. The server is stopped and started again on
the same port, and the test checks what an orchestrator then hears.

Run explicitly: python3 -m unittest tests_integration.test_restart_integration
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from tests_integration import base


PROJECT = "restartproj"
UNKNOWN = ("state unknown after the server restart (no hook event since; "
           "check the session)")


def tearDownModule():
    base.assert_test_tmux_footprint_gone()


@base.require_tools("git", "tmux", "backlog")
class ServerRestartIntegrationTests(base.IntegrationCase):
    def setUp(self):
        super().setUp()
        self.repo_path = base.init_backlog_repo(self.tmp_dir, name=PROJECT)
        self.app_dir = base.deploy_app(os.path.join(self.tmp_dir, "app"))
        self.state_home = os.path.join(self.tmp_dir, "state")
        self.port = base.free_port()
        base.write_projects_json(self.app_dir, {
            "port": self.port,
            "worktreeRoot": os.path.join(self.tmp_dir, "worktrees"),
            "projects": [{"name": PROJECT, "path": self.repo_path}],
        })

    def url(self, path, **query):
        return f"http://127.0.0.1:{self.port}{path}?" + urllib.parse.urlencode(query)

    def start_server(self):
        proc = subprocess.Popen(
            [sys.executable, "server.py"], cwd=self.app_dir,
            env=self.env({"XDG_STATE_HOME": self.state_home}),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
        )
        self.track_proc(proc)
        self.wait_for_http(self.url("/api/orchestrator-wait", project=PROJECT, timeout=0),
                           timeout=15.0)
        return proc

    def stop_server(self, proc):
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=10)

    def new_session(self, task):
        name = self.track_session(f"centrale-{PROJECT}-{task}")
        base.tmux("new-session", "-d", "-s", name, "sleep 300", check=True)
        return name

    def notify(self, task, state):
        event_url = self.url("/api/agent-event", project=PROJECT, task=task.upper(),
                             agentKind="claude")
        started = time.monotonic()
        subprocess.run([sys.executable, os.path.join(self.app_dir, "centrale_notify.py"), state],
                       env=self.env({"CENTRALE_EVENT_URL": event_url}), check=True,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)
        return time.monotonic() - started

    def wait(self, after=None, timeout=0):
        query = {"project": PROJECT, "timeout": timeout}
        if after is not None:
            query["after"] = after
        try:
            with urllib.request.urlopen(self.url("/api/orchestrator-wait", **query),
                                        timeout=timeout + 5) as response:
                return response.status, response.read().decode()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode()

    def drain(self, until, timeout=15.0):
        """Every line a fresh cursor hears, until `until` is among them."""
        cursor, lines = None, []
        deadline = time.monotonic() + timeout
        while until not in lines and time.monotonic() < deadline:
            status, body = self.wait(cursor, timeout=1)
            self.assertEqual(status, 200, body)
            position, message = body.rstrip("\n").split(" ", 1)
            if message != "nothing yet":
                lines.append(message)
            cursor = position
        return cursor, lines

    def fleet_agents(self):
        with urllib.request.urlopen(self.url("/api/fleet"), timeout=15) as response:
            return {a["taskId"]: a for a in json.loads(response.read())["agents"]}

    def test_restart_keeps_settled_states_and_announces_every_live_session(self):
        first = self.start_server()
        for task in ("task-1", "task-2", "task-3"):
            self.new_session(task)
        self.fleet_agents()  # a survey, as the dashboard's poll would make
        self.notify("task-1", "finished")
        self.notify("task-3", "finished")
        old_cursor, _ = self.drain("TASK-3 finished (ready to review)")
        since = self.fleet_agents()["TASK-1"]["stateSince"]
        self.assertIsNotNone(since)

        self.stop_server(first)
        # Fired while nothing listens: the hook must return at once.
        self.assertLess(self.notify("task-2", "finished"), 1.5)
        # TASK-3's session is replaced by a new one under the same name.
        base.tmux("kill-session", "-t", f"centrale-{PROJECT}-task-3", check=True)
        time.sleep(1.1)  # tmux records creation time in whole seconds
        self.new_session("task-3")
        self.start_server()

        status, body = self.wait(old_cursor)
        self.assertEqual(status, 409, body)
        _, lines = self.drain("TASK-2 finished (ready to review)")
        self.assertEqual(lines, [
            "TASK-1 finished (ready to review) (state before the server restart)",
            f"TASK-2 {UNKNOWN}",
            f"TASK-3 {UNKNOWN}",
            "TASK-2 finished (ready to review)",
        ])
        agents = self.fleet_agents()
        self.assertEqual((agents["TASK-1"]["state"], agents["TASK-1"]["stateSince"]),
                         ("finished", since))
        self.assertEqual(agents["TASK-2"]["state"], "finished")
        self.assertEqual(agents["TASK-3"]["state"], "unknown")
