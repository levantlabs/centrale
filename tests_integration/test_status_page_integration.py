"""task-214: the read-only status page reports the fleet the dashboard sees.

A real, unmodified server.py started the way the owner starts it --
`python3 server.py`, so the running module is `__main__`, not `server`.
The status page used to `import server`, which loaded server.py a second
time as a separate module with its own empty in-memory state: every agent
read as "unknown" there while GET /api/fleet showed the real states. The
hermetic suite imports server.py as `server` and so cannot see that split;
only a real process can.

Sessions are plain `sleep` panes on this run's tmux socket, hook events
come from the real centrale_notify.py, the fleet journal and the page's
key live under a private state directory, and the page listens on
127.0.0.1 on an ephemeral port.

Run explicitly: python3 -m unittest tests_integration.test_status_page_integration
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.parse
import urllib.request
from unittest import mock

from tests_integration import base


PROJECT = "statusproj"


def tearDownModule():
    base.assert_test_tmux_footprint_gone()


@base.require_tools("git", "tmux", "backlog")
class StatusPageIntegrationTests(base.IntegrationCase):
    def setUp(self):
        super().setUp()
        self.repo_path = base.init_backlog_repo(self.tmp_dir, name=PROJECT)
        self.app_dir = base.deploy_app(os.path.join(self.tmp_dir, "app"))
        self.state_home = os.path.join(self.tmp_dir, "state")
        self.key_file = os.path.join(self.tmp_dir, "status-page.key")
        self.port = base.free_port()
        self.status_port = base.free_port()
        self.track_port(self.status_port)

    def write_config(self, status_enabled):
        base.write_projects_json(self.app_dir, {
            "port": self.port,
            "worktreeRoot": os.path.join(self.tmp_dir, "worktrees"),
            "projects": [{"name": PROJECT, "path": self.repo_path}],
            "statusPage": {"enabled": status_enabled, "port": self.status_port, "bind": "127.0.0.1"},
        })

    def url(self, path, **query):
        return f"http://127.0.0.1:{self.port}{path}?" + urllib.parse.urlencode(query)

    def start_server(self):
        proc = subprocess.Popen(
            [sys.executable, "server.py"], cwd=self.app_dir,
            env=self.env({"XDG_STATE_HOME": self.state_home, "CENTRALE_STATUS_KEY_FILE": self.key_file}),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
        )
        self.track_proc(proc)
        self.wait_for_http(self.url("/api/board"), timeout=15.0)
        return proc

    def new_session(self, task):
        name = self.track_session(f"centrale-{PROJECT}-{task}")
        base.tmux("new-session", "-d", "-s", name, "sleep 300", check=True)
        return name

    def notify(self, task, state):
        event_url = self.url("/api/agent-event", project=PROJECT, task=task.upper(),
                             agentKind="claude")
        subprocess.run([sys.executable, os.path.join(self.app_dir, "centrale_notify.py"), state],
                       env=self.env({"CENTRALE_EVENT_URL": event_url}), check=True,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)

    def get_json(self, url):
        with urllib.request.urlopen(url, timeout=15) as response:
            return json.loads(response.read())

    def fleet(self):
        return self.get_json(self.url("/api/fleet"))

    def status_page(self):
        with open(self.key_file, encoding="utf-8") as f:
            key = f.read().strip()
        return self.get_json(f"http://127.0.0.1:{self.status_port}/api/status?"
                             + urllib.parse.urlencode({"key": key}))

    def settle_fleet(self):
        """Two hook states on two live sessions, one of each kind the
        page has to tell apart, and the dashboard's view of them."""
        for task in ("task-1", "task-2"):
            self.new_session(task)
        self.fleet()  # a survey, as the dashboard's poll would make
        self.notify("task-1", "finished")
        self.notify("task-2", "working")
        base.wait_until(
            lambda: {a["taskId"]: a["state"] for a in self.fleet()["agents"]}
            == {"TASK-1": "finished", "TASK-2": "working"},
            message="the dashboard never saw both hook states")

    def assert_page_matches_fleet(self):
        fleet = self.fleet()
        page = self.status_page()

        def agents(snapshot):
            return sorted((a["project"], a["taskId"], a["agent"], a["state"], a["stateSince"])
                          for a in snapshot["agents"])

        def history(snapshot):
            return [(r["project"], r["taskId"], r["agent"], r["state"], r["timestamp"])
                    for r in snapshot["history"]]

        self.assertEqual(agents(page), agents(fleet))
        self.assertEqual(history(page), history(fleet))
        states = {a[1]: a[3] for a in agents(page)}
        self.assertEqual(states, {"TASK-1": "finished", "TASK-2": "working"})
        self.assertNotIn("unknown", {a[2] for a in agents(page)})
        self.assertTrue(all(a[4] is not None for a in agents(page)))
        self.assertIn(("statusproj", "TASK-1", "claude", "finished"),
                      {r[:4] for r in history(page)})

    def test_page_started_at_startup_reads_the_running_fleet(self):
        self.write_config(status_enabled=True)
        self.start_server()
        self.settle_fleet()
        self.assert_page_matches_fleet()

    def test_page_started_from_settings_reads_the_running_fleet(self):
        self.write_config(status_enabled=False)
        self.start_server()
        self.assertFalse(base.port_listening(self.status_port))
        self.settle_fleet()
        req = urllib.request.Request(
            self.url("/api/settings"), method="POST",
            data=json.dumps({"statusPage": {"enabled": True, "port": self.status_port,
                                            "bind": "127.0.0.1"}}).encode("utf-8"),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as response:
            result = json.loads(response.read())
        self.assertTrue(result["statusPage"]["running"], result["statusPage"])
        self.assert_page_matches_fleet()

    def test_dashboard_spawn_lands_in_the_running_fleet(self):
        # spawn.py records the spawn through `server`; with a second copy
        # of server.py that row went to the copy's journal and the
        # dashboard showed no spawn and an "unknown" agent.
        repo_task = base.create_task(self.repo_path, "Spawned from the dashboard", acceptance_criteria=["x"])
        base.run(["git", "add", "backlog"], cwd=self.repo_path)
        base.run(["git", "commit", "-q", "-m", f"create {repo_task}"], cwd=self.repo_path)
        self.write_config(status_enabled=True)
        with mock.patch.dict(os.environ, {"CENTRALE_SPAWN_CMD": base.probe_spawn_cmd(marker="itest-status-page")}):
            self.start_server()
        req = urllib.request.Request(
            self.url("/api/spawn"), method="POST",
            data=json.dumps({"project": PROJECT, "taskId": repo_task}).encode("utf-8"),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=60) as response:
            self.track_session(json.loads(response.read())["session"])
        fleet = self.fleet()
        self.assertIn((repo_task, "custom", "spawn"),
                      {(r["taskId"], r["agent"], r["state"]) for r in fleet["history"]})
        self.assertEqual([(a["taskId"], a["agent"]) for a in fleet["agents"]], [(repo_task, "custom")])
        self.assertEqual(self.status_page()["history"], fleet["history"])
