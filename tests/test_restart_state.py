"""What survives a server restart (task-201): journal-restored badges,
the boot-time orchestrator announcement, and hook-event ordering.

Hermetic: an isolated journal file per test, no tmux, no subprocesses.
"""

import http.client
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
import uuid
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import fleet
import orchestrator
import server


CREATED = int(time.time()) - 3600  # inside the journal's 48 h retention


class RestartStateTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = os.path.join(tmp.name, "fleet.jsonl")
        self.addCleanup(server._reset_agent_events)
        self.fresh()

    def fresh(self):
        """A new project and an empty journal, for each subTest."""
        if os.path.exists(self.path):
            os.unlink(self.path)
        self.project = uuid.uuid4().hex
        self.config = {"worktreeRoot": "/unused",
                       "projects": [{"name": self.project, "path": "/unused"}]}
        self.journal(fleet.Journal(self.path))
        server._reset_agent_events()

    def journal(self, journal):
        patcher = mock.patch.object(server, "fleet_history", journal)
        patcher.start()
        self.addCleanup(patcher.stop)
        return journal

    def session(self, task="task-2", created=CREATED):
        return {"name": f"centrale-{self.project}-{task}", "created": str(created),
                "attached": False}

    def restart(self, sessions):
        """What main() does with a fresh process: reload the journal from
        disk, forget every in-memory badge, then restore and announce."""
        server._reset_agent_events()
        self.journal(fleet.Journal(self.path))
        server.observe_fleet_sessions(sessions, self.config)
        restored = server.restore_agent_states(self.config, sessions)
        server.announce_restart_states(self.config, sessions)
        return restored

    def append(self, state, timestamp, task="TASK-2", **details):
        server.fleet_history.append(self.project, task, "claude", state,
                                    timestamp=timestamp, **details)

    def lines(self):
        cursor, out = None, []
        while True:
            line = orchestrator.wait(self.project, cursor, timeout=0)
            cursor, message = line.rstrip("\n").split(" ", 1)
            if message == "nothing yet":
                return out
            out.append(message)

    # -- AC #1: restore only onto the same session instance -------------

    def test_settled_states_come_back_with_their_original_since(self):
        for state, kind, public_kind in (("finished", "claude", "claude"),
                                         ("waiting", "claude", "claude"),
                                         ("idle", "codex", "codex")):
            with self.subTest(state=state):
                self.fresh()
                self.append(state, CREATED + 30, agentKind=kind)
                self.restart([self.session()])
                lifecycle = server.get_fleet_lifecycle(self.project, "task-2")
                self.assertEqual(lifecycle["state"], state)
                self.assertEqual(lifecycle["stateSince"], CREATED + 30)
                self.assertEqual(server.get_agent_kind(self.project, "task-2"), public_kind)

    def test_session_recreated_under_the_same_name_does_not_inherit(self):
        self.append("finished", CREATED + 30)
        self.assertEqual(self.restart([self.session(created=CREATED + 60)]), [])
        self.assertEqual(server.get_agent_state(self.project, "task-2"), "unknown")

    def test_a_row_naming_another_session_instance_is_not_restored(self):
        self.append("finished", CREATED + 30, created=str(CREATED - 5))
        self.restart([self.session()])
        self.assertEqual(server.get_agent_state(self.project, "task-2"), "unknown")

    def test_working_spawn_and_ended_sessions_stay_unknown(self):
        for state in ("working", "spawn", "session ended"):
            with self.subTest(state=state):
                self.fresh()
                self.append("finished", CREATED + 10)
                self.append(state, CREATED + 30)
                self.restart([self.session()])
                self.assertEqual(server.get_agent_state(self.project, "task-2"), "unknown")

    def test_merge_rows_do_not_hide_the_agent_state(self):
        self.append("finished", CREATED + 10)
        self.append("merge blocked", CREATED + 20)
        self.restart([self.session()])
        self.assertEqual(server.get_agent_state(self.project, "task-2"), "finished")

    def test_rows_written_by_a_running_server_carry_kind_and_session(self):
        server.observe_fleet_sessions([self.session()], self.config)
        server.record_agent_event(self.project, "task-2", "finished", agent_kind="claude")
        row = server.fleet_history.last_agent_state(self.project, "task-2")
        self.assertEqual((row["agentKind"], row["created"]), ("claude", str(CREATED)))
        self.restart([self.session()])
        self.assertEqual(server.get_agent_state(self.project, "task-2"), "finished")
        self.assertEqual(server.get_agent_kind(self.project, "task-2"), "claude")

    def test_restore_writes_no_journal_row_and_a_new_event_still_wins(self):
        self.append("finished", CREATED + 30)
        self.restart([self.session()])
        rows = len(server.fleet_history.rows)
        server.record_agent_event(self.project, "task-2", "working", agent_kind="claude")
        self.assertEqual(server.get_agent_state(self.project, "task-2"), "working")
        self.assertEqual(len(server.fleet_history.rows), rows + 1)

    # -- AC #2/#3: a fresh wait hears every live session ---------------

    def test_fresh_wait_hears_every_live_session_once(self):
        self.append("finished", CREATED + 30, task="TASK-2")
        self.append("working", CREATED + 30, task="TASK-3")
        self.restart([self.session("task-2"), self.session("task-3"),
                      {"name": "centrale-elsewhere-task-9", "created": str(CREATED)}])
        self.assertEqual(self.lines(), [
            "TASK-2 finished (ready to review) (state before the server restart)",
            "TASK-3 state unknown after the server restart (no hook event since; "
            "check the session)",
        ])

    def test_pre_restart_cursor_is_refused_and_a_fresh_wait_recovers(self):
        before = orchestrator.wait(self.project, None, timeout=0).split(" ", 1)[0]
        with mock.patch.dict(orchestrator._streams, clear=True):
            self.append("waiting", CREATED + 30)
            self.restart([self.session()])
            with self.assertRaises(orchestrator.WaitError) as caught:
                orchestrator.wait(self.project, before, timeout=0)
            self.assertEqual(caught.exception.status, 409)
            self.assertEqual(self.lines(),
                             ["TASK-2 waiting for input (state before the server restart)"])


class FiredAtOrderingTests(unittest.TestCase):
    """A retried hook can arrive after a later one (AC #4's retry)."""

    def setUp(self):
        server._reset_agent_events()
        self.addCleanup(server._reset_agent_events)
        patcher = mock.patch.object(server, "fleet_history", fleet.Journal())
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_an_event_fired_before_the_last_applied_one_is_dropped(self):
        now = time.time()
        server.record_agent_event("p", "TASK-1", "finished", "claude", fired_at=now - 1)
        server.record_agent_event("p", "TASK-1", "working", "claude", fired_at=now - 2)
        self.assertEqual(server.get_agent_state("p", "TASK-1"), "finished")
        server.record_agent_event("p", "TASK-1", "working", "claude", fired_at=now)
        self.assertEqual(server.get_agent_state("p", "TASK-1"), "working")

    def test_events_without_fired_at_keep_arrival_order(self):
        server.record_agent_event("p", "TASK-1", "finished", "claude", fired_at=time.time())
        server.record_agent_event("p", "TASK-1", "working", "claude")
        self.assertEqual(server.get_agent_state("p", "TASK-1"), "working")

    def test_a_retried_event_from_before_a_respawn_is_dropped(self):
        fired = time.time() - 1
        server.clear_agent_event("p", "TASK-1", agent_name="claude")
        server.record_agent_event("p", "TASK-1", "finished", "claude", fired_at=fired)
        self.assertEqual(server.get_agent_state("p", "TASK-1"), "unknown")

    def test_a_future_stamp_cannot_shadow_later_events(self):
        server.record_agent_event("p", "TASK-1", "finished", "claude",
                                  fired_at=time.time() + 3600)
        time.sleep(0.01)
        server.record_agent_event("p", "TASK-1", "working", "claude", fired_at=time.time())
        self.assertEqual(server.get_agent_state("p", "TASK-1"), "working")


class FiredAtHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = {"worktreeRoot": "/unused", "projects": [{"name": "p", "path": "/unused"}]}
        cls.httpd = server.CentraleHTTPServer(("127.0.0.1", 0), server.Handler, cls.config)
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join(timeout=5)

    def setUp(self):
        server._reset_agent_events()
        self.addCleanup(server._reset_agent_events)

    def post(self, body):
        conn = http.client.HTTPConnection("127.0.0.1", self.httpd.server_address[1], timeout=5)
        try:
            query = urllib.parse.urlencode({"project": "p", "task": "TASK-1", "agentKind": "claude"})
            conn.request("POST", "/api/agent-event?" + query, body=json.dumps(body),
                         headers={"Content-Type": "application/json"})
            return conn.getresponse().status
        finally:
            conn.close()

    def test_fired_at_must_be_a_finite_number(self):
        for bad in ("soon", True, [1]):
            with self.subTest(bad=bad):
                self.assertEqual(self.post({"state": "finished", "firedAt": bad}), 400)
        self.assertEqual(server.get_agent_state("p", "TASK-1"), "unknown")

    def test_out_of_order_posts_keep_the_later_fired_state(self):
        now = time.time()
        with mock.patch.object(server, "fleet_history", fleet.Journal()):
            self.assertEqual(self.post({"state": "finished", "firedAt": now - 1}), 200)
            self.assertEqual(self.post({"state": "working", "firedAt": now - 2}), 200)
        self.assertEqual(server.get_agent_state("p", "TASK-1"), "finished")


if __name__ == "__main__":
    unittest.main()
