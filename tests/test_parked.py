"""PARKED (task-170.3): idle past a threshold behind a dialog or an
undelivered message. Panes are the REAL claude/codex captures from
tests/fixtures/panes; tmux, the clock and the delivery log are injected."""
import unittest
from pathlib import Path
from unittest import mock

import server

PANES = Path(__file__).parent / "fixtures" / "panes"
NOW = 10_000.0
CONFIG = {"projects": [{"name": "app", "path": "/repo"}], "sessionPreview": {"mode": "interact"}}
SESSION = {"name": "centrale-app-task-1", "created": "1000"}


def pane(name):
    return (PANES / name).read_text().split("\n")


def status(pane_name, state="idle", since=NOW - 600, deliveries=(), config=CONFIG):
    with mock.patch.object(server, "capture_session_pane", return_value=pane(pane_name)):
        return server.parked_status(config, SESSION, state, since, list(deliveries), now=NOW)


def failed(outcome="dialog", at="1970-01-01T01:00:00Z"):
    return {"project": "app", "task": "TASK-1", "outcome": outcome, "time": at}


class ParkedStatusTests(unittest.TestCase):
    def test_dialog_past_threshold_is_parked_for_claude_and_codex(self):
        for fixture, state in (("claude-trust-dialog.txt", "unknown"), ("claude-trust-dialog.txt", "finished"),
                               ("codex-trust-dialog.txt", "idle"), ("codex-trust-dialog.txt", "unknown")):
            with self.subTest(fixture=fixture, state=state):
                result = status(fixture, state)
                self.assertIn("dialog", result["reason"])
                self.assertTrue(result["lastLine"])
                self.assertEqual(result["lastLine"], result["lastLine"].strip())

    def test_finished_agent_with_clean_pane_is_not_parked(self):
        for fixture, state in (("claude-idle-after-reply.txt", "finished"),
                               ("codex-idle-after-reply.txt", "idle")):
            with self.subTest(fixture=fixture):
                self.assertIsNone(status(fixture, state))

    def test_under_threshold_is_not_parked_and_never_captures(self):
        with mock.patch.object(server, "capture_session_pane") as capture:
            self.assertIsNone(server.parked_status(CONFIG, SESSION, "idle", NOW - 60, [], now=NOW))
            self.assertIsNone(server.parked_status(
                {**CONFIG, "parkedAfterSeconds": 30}, SESSION, "idle", NOW - 20, [], now=NOW))
        capture.assert_not_called()
        self.assertIsNotNone(status("claude-trust-dialog.txt", config={**CONFIG, "parkedAfterSeconds": 30}))

    def test_working_and_waiting_are_never_parked(self):
        for state in ("working", "waiting"):
            self.assertIsNone(status("claude-trust-dialog.txt", state))

    def test_undelivered_message_parks_a_clean_pane(self):
        result = status("claude-idle-after-reply.txt", "finished", deliveries=[failed("no-echo")])
        self.assertIn("message not delivered (no-echo)", result["reason"])
        self.assertTrue(result["lastLine"])

    def test_latest_delivery_wins_and_old_session_messages_do_not_count(self):
        ok = {**failed(), "outcome": "delivered", "time": "1970-01-01T02:00:00Z"}
        self.assertIsNone(status("claude-idle-after-reply.txt", "finished", deliveries=[failed(), ok]))
        # Sent at 00:10 (t=600), before this session started at t=1000.
        stale = failed(at="1970-01-01T00:10:00Z")
        self.assertIsNone(status("claude-idle-after-reply.txt", "finished", deliveries=[stale]))
        other = {**failed(), "task": "TASK-2"}
        self.assertIsNone(status("claude-idle-after-reply.txt", "finished", deliveries=[other]))

    def test_unknown_state_ages_from_session_start(self):
        result = status("claude-trust-dialog.txt", "unknown", since=None)
        self.assertEqual(result["since"], 1000.0)

    def test_preview_off_never_reads_the_pane_but_delivery_still_counts(self):
        config = {**CONFIG, "sessionPreview": {"mode": "off"}}
        with mock.patch.object(server, "capture_session_pane") as capture:
            self.assertIsNone(server.parked_status(config, SESSION, "idle", NOW - 600, [], now=NOW))
            result = server.parked_status(config, SESSION, "idle", NOW - 600, [failed()], now=NOW)
        capture.assert_not_called()
        self.assertIsNone(result["lastLine"])

    def test_unreadable_pane_is_not_a_dialog(self):
        with mock.patch.object(server, "capture_session_pane",
                               side_effect=server.PaneCaptureError("gone", status=404)):
            self.assertIsNone(server.parked_status(CONFIG, SESSION, "idle", NOW - 600, [], now=NOW))


class ParkedConfigTests(unittest.TestCase):
    def test_normalize(self):
        self.assertEqual(server.normalize_parked_after(None), server.DEFAULT_PARKED_AFTER_SECONDS)
        self.assertEqual(server.normalize_parked_after(60), 60)
        for bad in (True, "60", 1.5, 9, -1):
            with self.subTest(bad=bad), self.assertRaises(server.ConfigError):
                server.normalize_parked_after(bad)


class ParkedSurfacesTests(unittest.TestCase):
    def lifecycle(self, state="idle"):
        return {"state": state, "stateSince": NOW - 600, "agent": "helper", "agentKind": "codex"}

    def test_sessions_enrichment_marks_parked(self):
        sessions = [dict(SESSION)]
        with mock.patch.object(server, "capture_session_pane", return_value=pane("codex-trust-dialog.txt")), \
             mock.patch.object(server, "get_fleet_lifecycle", return_value=self.lifecycle()), \
             mock.patch.object(server, "read_delivery_log", return_value=([], 0)), \
             mock.patch.object(server.time, "time", return_value=NOW), \
             mock.patch.object(server, "_collect_touched_files", return_value=(None, None)):
            server.enrich_sessions_with_files(sessions, {**CONFIG, "worktreeRoot": "/nowhere"})
        self.assertIn("dialog", sessions[0]["parked"]["reason"])

    def test_inbox_parked_item_replaces_the_idle_one(self):
        agent = {"project": "app", "taskId": "TASK-1", "session": SESSION["name"], "agent": "helper",
                 "state": "idle", "stateSince": NOW - 600,
                 "parked": {"reason": "dialog on the pane", "since": NOW - 600,
                            "lastLine": "Press enter to continue", "dialog": "x"}}
        with mock.patch.object(server, "get_board", return_value={"projects": []}), \
             mock.patch.object(server, "_task_view_or_none", side_effect=AssertionError("idle report read")):
            items, _ = server.fleet_inbox({**CONFIG, "worktreeRoot": "/nowhere"},
                                          {"agents": [agent], "timestamp": NOW}, [])
        parked = [i for i in items if i["kind"] == "parked"]
        self.assertEqual(len(parked), 1)
        self.assertEqual(parked[0]["text"], "Press enter to continue")
        self.assertEqual([i["kind"] for i in items if i["kind"] == "idle"], [])


if __name__ == "__main__":
    unittest.main()
