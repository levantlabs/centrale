"""Remote machines in the API and Fleet (doc-3 "API and UI", task-210).

Hermetic: fake control connections and a faked machines.refresh; no ssh, no tmux.
"""
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import fleet
import machines
import remote_status as R
import server

BOX = {"name": "box-b", "ssh": "u@box-b", "repoRoot": "~/r", "worktreeRoot": "~/w", "maxAgents": 8}


class FakeConnection:
    def __init__(self, reach, tasks=()):
        self.reach, self._tasks = reach, list(tasks)

    def tasks(self):
        return self._tasks


def config(with_machines=True):
    cfg = {"projects": [{"name": "app", "path": "/repo", "machines": ["box-b"] if with_machines else []},
                        {"name": "local", "path": "/local"}],
           "worktreeRoot": "/tmp/wt", "sessionPreview": {"mode": "off"}}
    if with_machines:
        cfg["machines"] = [BOX]
    return cfg


class RemoteSessionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.journal = fleet.Journal(os.path.join(self.tmp.name, "h.jsonl"))
        for patch in (mock.patch.object(server, "fleet_history", self.journal),
                      mock.patch.object(server, "OWNER_QUESTIONS_TTL", 0.0),
                      mock.patch.object(server, "list_sessions",
                                        return_value=[{"name": "centrale-local-task-2", "created": "100",
                                                       "attached": False}]),
                      mock.patch.object(server, "read_delivery_log", return_value=([], 0)),
                      mock.patch.object(server, "_collect_touched_files", return_value=(None, None)),
                      mock.patch.object(machines, "refresh", side_effect=self.refresh)):
            patch.start()
            self.addCleanup(patch.stop)
        server._reset_agent_events()
        self.addCleanup(server._reset_agent_events)
        self.reach = "reachable"
        self.addCleanup(R._connections.clear)

    def refresh(self, cfg, reads=None):
        return [{"name": "box-b", "reach": self.reach, "error": None, "results": []}]

    def connect(self, reach, tasks=()):
        self.reach = reach
        R._connections["box-b"] = FakeConnection(reach, tasks)

    def test_reachable_machine_sessions_carry_machine_and_worktree(self):
        self.connect("reachable", [("app", "TASK-7", "claude")])
        server.record_agent_event("app", "task-7", "working", agent_kind="claude")
        rows = server.remote_session_rows(config())
        self.assertEqual(rows, [{"name": "centrale-app-task-7", "created": None, "attached": False,
                                 "machine": "box-b", "reach": "reachable",
                                 "worktree": "~/w/app-task-7",
                                 "attach": "ssh -t u@box-b tmux attach -t centrale-app-task-7"}])
        server.enrich_sessions_with_files(rows, config())
        self.assertEqual((rows[0]["project"], rows[0]["agentState"]), ("app", "working"))
        self.assertNotIn("files", rows[0])

    def test_unreachable_machine_agents_read_unreachable_not_unknown(self):
        self.journal.append("app", "TASK-7", "helper", "working")
        self.connect("unreachable")
        # task-224: the hub's fetched ref is what places TASK-7 on box-b.
        fetched = lambda args, cwd=None: subprocess.CompletedProcess(  # noqa: E731
            args, 0 if args[-1] == "refs/remotes/box-b/task/task-7" else 1, "", "")
        patcher = mock.patch.object(server, "run_git", fetched)
        patcher.start()
        self.addCleanup(patcher.stop)
        rows = server.remote_session_rows(config())
        self.assertEqual([r["reach"] for r in rows], ["unreachable"])
        server.enrich_sessions_with_files(rows, config())
        self.assertEqual(rows[0]["agentState"], "unreachable")
        snapshot = server.get_fleet(config())
        remote = [a for a in snapshot["agents"] if a["project"] == "app"]
        self.assertEqual([(a["state"], a["machine"]) for a in remote], [("unreachable", "box-b")])

    def test_fleet_counts_capacity_per_machine(self):
        self.connect("reachable", [("app", "TASK-7", "claude"), ("app", "TASK-8", "codex")])
        snapshot = server.get_fleet(config())
        self.assertEqual(snapshot["machines"][0]["agentCount"], 2)
        self.assertEqual(snapshot["machines"][0]["maxAgents"], 8)
        self.assertEqual(snapshot["hub"], {"agentCount": 1})
        counts = {p["name"]: p["agentCount"] for p in snapshot["projects"]}
        self.assertEqual(counts, {"app": 2, "local": 1})
        local = [a for a in snapshot["agents"] if a["project"] == "local"][0]
        self.assertNotIn("machine", local)

    def test_single_machine_is_untouched(self):
        with mock.patch.dict(sys.modules, {"remote_status": None}):
            self.assertEqual(server.remote_session_rows(config(False)), [])
        snapshot = server.get_fleet(config(False))
        self.assertNotIn("machines", snapshot)
        self.assertNotIn("hub", snapshot)
        self.assertEqual(server.machine_fields(config(False), config(False)["projects"][0], "TASK-1"), {})
        sessions = server.list_sessions()
        server.enrich_sessions_with_files(sessions, config(False))
        self.assertFalse({"machine", "worktree", "reach"} & set(sessions[0]))

    def test_task_fields_name_machine_and_worktree_for_remote_projects_only(self):
        cfg = config()
        self.assertEqual(server.machine_fields(cfg, machines.placed(cfg["projects"][0], "box-b"), "TASK-7"),
                         {"machine": "box-b", "worktree": "~/w/app-task-7"})
        self.assertEqual(server.machine_fields(cfg, machines.placed(cfg["projects"][0], None), "TASK-7"), {})
        self.assertEqual(server.machine_fields(cfg, cfg["projects"][1], "TASK-7"), {})


if __name__ == "__main__":
    unittest.main()
