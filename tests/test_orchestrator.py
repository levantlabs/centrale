"""The optional waiting API, driven through real HTTP without subprocesses."""

import concurrent.futures
import http.client
import io
import json
import os
import subprocess
import sys
import threading
import time
import unittest
import urllib.parse
import uuid
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import centrale_notify
import harvest
import server
import spawn

from lifecycle_harness import ManualTimer


class OrchestratorHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = {"projects": []}
        cls.httpd = server.CentraleHTTPServer(("127.0.0.1", 0), server.Handler, cls.config)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join(timeout=5)

    def setUp(self):
        self.project = uuid.uuid4().hex
        self.other = uuid.uuid4().hex
        self.config["worktreeRoot"] = "/unused"
        self.config["projects"] = [{"name": name, "path": "/unused"}
                                   for name in (self.project, self.other)]
        server._reset_agent_events()
        harvest._reset_events()
        self.timers = []
        def timer(interval, callback):
            value = ManualTimer(interval, callback)
            self.timers.append(value)
            return value
        patcher = mock.patch.object(server, "_agent_wait_timer", side_effect=timer, create=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(server._reset_agent_events)
        # No real tmux: a hook that stays pending is corroborated by the
        # rendered approval dialog, not by the age of a stale hook alone.
        capture = mock.patch.object(server, "capture_session_pane", return_value=[
            "Allow command?", "Press enter to confirm or esc to cancel",
        ])
        self.capture = capture.start()
        self.addCleanup(capture.stop)

    def confirm_stop(self):
        with mock.patch.object(server, "capture_session_pane", return_value=["› Ask Codex to do anything"]):
            self.timers[-1].fire()

    def request(self, path, method="GET", body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            conn.request(method, path, body=body, headers=headers or {})
            response = conn.getresponse()
            return response.status, response.read().decode(), dict(response.getheaders())
        finally:
            conn.close()

    def wait(self, after=None, timeout=0, project=None):
        query = {"project": project or self.project, "timeout": timeout}
        if after is not None:
            query["after"] = after
        return self.request("/api/orchestrator-wait?" + urllib.parse.urlencode(query))

    def post_state(self, state, task="TASK-2", kind="claude", project=None):
        query = urllib.parse.urlencode({"project": project or self.project,
                                       "task": task, "agentKind": kind})
        status, _, _ = self.request("/api/agent-event?" + query, "POST",
                                    json.dumps({"state": state}),
                                    {"Content-Type": "application/json"})
        self.assertEqual(status, 200)

    def line(self, response, expected):
        status, body, headers = response
        self.assertEqual(status, 200, body)
        self.assertEqual(headers["Content-Type"], "text/plain; charset=utf-8")
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(body.count("\n"), 1, body)
        cursor, message = body.rstrip("\n").split(" ", 1)
        self.assertEqual(message, expected)
        return cursor

    def test_replays_events_between_calls_once_in_order(self):
        self.post_state("finished")
        self.post_state("waiting", task="task-3.1")
        first = self.line(self.wait(), "TASK-2 finished (ready to review)")
        second = self.line(self.wait(after=first), "TASK-3.1 waiting for input")
        self.assertNotEqual(first, second)
        self.assertEqual(self.line(self.wait(after=second), "nothing yet"), second)

    def test_duplicate_hooks_are_suppressed_but_new_turns_are_not(self):
        self.post_state("finished")
        self.post_state("finished")
        cursor = self.line(self.wait(), "TASK-2 finished (ready to review)")
        self.line(self.wait(after=cursor), "nothing yet")
        self.post_state("working")
        self.post_state("finished")
        self.line(self.wait(after=cursor), "TASK-2 finished (ready to review)")

    def test_codex_idle_is_not_finished_and_known_kind_is_retained(self):
        self.post_state("finished", kind="codex")
        server.record_agent_event(self.project, "TASK-2", "finished")
        self.confirm_stop()
        cursor = self.line(self.wait(), "TASK-2 idle (turn ended, may need input)")
        self.line(self.wait(after=cursor), "nothing yet")
        self.post_state("waiting", kind="codex")
        self.line(self.wait(after=cursor), "nothing yet")
        self.timers[-1].fire()
        cursor = self.line(self.wait(after=cursor), "TASK-2 waiting for input")
        self.post_state("working", kind="codex")
        self.post_state("finished", kind="codex")
        self.confirm_stop()
        self.line(self.wait(after=cursor), "TASK-2 idle (turn ended, may need input)")

    def test_nested_stop_stays_working_until_pane_really_settles(self):
        self.capture.return_value = ["• Working (2m 17s • esc to interrupt)", "›"]
        self.post_state("working", kind="codex")
        self.post_state("finished", kind="codex")
        pending = self.timers[-1]
        self.assertEqual(pending.interval, 1)
        self.assertTrue(pending.daemon)
        self.post_state("finished", kind="codex")  # duplicate notify
        self.assertIs(self.timers[-1], pending)
        pending.fire()
        self.capture.assert_called_with(spawn.session_name(self.project, "TASK-2"), 0)
        self.assertEqual(server.get_agent_state(self.project, "TASK-2"), "working")
        cursor = self.line(self.wait(), "nothing yet")
        self.assert_inbox([])
        self.confirm_stop()
        self.assertEqual(server.get_agent_state(self.project, "TASK-2"), "idle")
        cursor = self.line(self.wait(after=cursor), "TASK-2 idle (turn ended, may need input)")
        self.assert_inbox(["idle"])
        self.timers[-1].fire()  # spent callback cannot publish twice
        self.line(self.wait(after=cursor), "nothing yet")

    def assert_inbox(self, kinds):
        with mock.patch.object(server, "list_sessions", return_value=[{
                "name": spawn.session_name(self.project, "TASK-2"), "created": 1}]), \
             mock.patch.object(server, "read_delivery_log", return_value=([], 0)), \
             mock.patch.object(server, "_task_view_or_none", return_value={
                 "task": {"status": "In Progress", "finalSummary": ""}}):
            status, body, _ = self.request("/api/fleet")
        self.assertEqual(status, 200, body)
        self.assertEqual([item["kind"] for item in json.loads(body)["needsYou"]], kinds)

    def test_new_activity_clear_reset_or_kind_change_invalidates_stop(self):
        for action in ("working", "waiting", "clear", "reset", "kind"):
            with self.subTest(action=action):
                self.post_state("finished", kind="codex")
                pending = self.timers[-1]
                if action in ("working", "waiting"):
                    self.post_state(action, kind="codex")
                elif action == "clear":
                    server.clear_agent_event(self.project, "TASK-2")
                elif action == "reset":
                    server._reset_agent_events()
                else:
                    self.post_state("working", kind="claude")
                pending.fire()
                self.assertTrue(pending.cancelled)
                self.line(self.wait(), "nothing yet")

    def test_activity_during_stop_capture_cannot_publish_idle(self):
        self.post_state("finished", kind="codex")
        def capture(*args):
            self.post_state("working", kind="codex")
            return ["› Ask Codex to do anything"]
        self.capture.side_effect = capture
        self.timers[-1].fire()
        self.assertEqual(server.get_agent_state(self.project, "TASK-2"), "working")
        self.line(self.wait(), "nothing yet")

    def test_stop_with_real_dialog_publishes_waiting_on_first_check(self):
        self.post_state("finished", kind="codex")
        self.timers[-1].fire()
        self.assertEqual(server.get_agent_state(self.project, "TASK-2"), "waiting")
        self.line(self.wait(), "TASK-2 waiting for input")

    def test_stop_capture_failure_is_unknown_and_retries_except_gone_session(self):
        for status in (500, 404):
            with self.subTest(status=status):
                self.post_state("working", kind="codex")
                self.post_state("finished", kind="codex")
                pending = self.timers[-1]
                self.capture.side_effect = server.PaneCaptureError("capture failed", status=status)
                pending.fire()
                self.assertEqual(server.get_agent_state(self.project, "TASK-2"), "unknown")
                self.line(self.wait(), "nothing yet")
                self.assertEqual(self.timers[-1] is pending, status == 404)
                self.post_state("working", kind="codex")
        self.capture.side_effect = None

    def test_empty_stop_capture_cannot_claim_idle_and_recovery_is_prompt(self):
        self.post_state("finished", kind="codex")
        self.capture.return_value = []
        self.timers[-1].fire()
        self.assertEqual(server.get_agent_state(self.project, "TASK-2"), "unknown")
        self.line(self.wait(), "nothing yet")
        self.confirm_stop()
        self.line(self.wait(), "TASK-2 idle (turn ended, may need input)")

    def test_old_working_text_above_footer_cannot_hide_real_stop(self):
        self.post_state("finished", kind="codex")
        self.capture.return_value = ["• Working (1s • esc to interrupt)"] + ["response text"] * 12 + ["›"]
        self.timers[-1].fire()
        self.line(self.wait(), "TASK-2 idle (turn ended, may need input)")

    def test_new_stop_candidate_cannot_be_completed_by_old_capture(self):
        self.post_state("finished", kind="codex")
        old = self.timers[-1]
        self.post_state("working", kind="codex")
        self.post_state("finished", kind="codex")
        old.fire()
        self.line(self.wait(), "nothing yet")
        self.confirm_stop()
        self.line(self.wait(), "TASK-2 idle (turn ended, may need input)")

    def test_claude_idle_reminder_cannot_overwrite_working_or_finished(self):
        url = f"http://127.0.0.1:{self.port}/api/agent-event?" + urllib.parse.urlencode({
            "project": self.project, "task": "TASK-2", "agentKind": "claude"})
        cursor = None
        for state in ("finished", "working"):
            self.post_state(state)
            if state == "finished":
                cursor = self.line(self.wait(), "TASK-2 finished (ready to review)")
            with mock.patch.dict(os.environ, {"CENTRALE_EVENT_URL": url}), \
                 mock.patch.object(sys, "stdin", io.StringIO('{"notification_type":"idle_prompt"}')):
                centrale_notify.main(["notify", "notification"])
            self.assertEqual(server.get_agent_state(self.project, "TASK-2"), state)
            self.line(self.wait(after=cursor), "nothing yet")
            self.assert_inbox([])
        for kind in ("permission_prompt", "elicitation_dialog", "agent_needs_input"):
            self.post_state("working")
            with mock.patch.dict(os.environ, {"CENTRALE_EVENT_URL": url}), \
                 mock.patch.object(sys, "stdin", io.StringIO(json.dumps({"notification_type": kind}))):
                centrale_notify.main(["notify", "notification"])
            self.assertEqual(server.get_agent_state(self.project, "TASK-2"), "waiting")
            cursor = self.line(self.wait(after=cursor), "TASK-2 waiting for input")
            self.assert_inbox(["permission"])

    def test_transient_codex_permission_never_publishes_or_changes_badge_to_waiting(self):
        self.post_state("working", kind="codex")
        self.post_state("waiting", kind="codex")
        self.assertEqual(server.get_agent_state(self.project, "TASK-2"), "working")
        cursor = self.line(self.wait(), "nothing yet")
        self.post_state("working", kind="codex")
        self.timers[0].fire()
        self.assertEqual(server.get_agent_state(self.project, "TASK-2"), "working")
        self.line(self.wait(after=cursor), "nothing yet")

    def test_persistent_codex_permission_publishes_once_and_duplicates_keep_deadline(self):
        self.post_state("waiting", kind="codex")
        self.assertEqual(server.get_agent_lifecycle(self.project, "TASK-2"),
                         {"agentState": "working", "agentKind": "codex"})
        cursor = self.line(self.wait(), "nothing yet")
        self.post_state("waiting", kind="codex")
        self.assertEqual(len(self.timers), 1)
        self.assertEqual(self.timers[0].interval, 30)
        self.assertTrue(self.timers[0].daemon)
        self.timers[0].fire()
        self.assertEqual(server.get_agent_state(self.project, "TASK-2"), "waiting")
        cursor = self.line(self.wait(after=cursor), "TASK-2 waiting for input")
        self.timers[0].fire()
        self.post_state("waiting", kind="codex")
        self.line(self.wait(after=cursor), "nothing yet")

    def test_cancelled_timer_cannot_publish_for_a_new_permission_request(self):
        self.post_state("waiting", kind="codex")
        self.post_state("working", kind="codex")
        self.post_state("waiting", kind="codex")
        self.assertEqual(server.get_agent_state(self.project, "TASK-2"), "working")
        self.timers[0].fire()
        self.line(self.wait(), "nothing yet")
        self.timers[1].fire()
        self.line(self.wait(), "TASK-2 waiting for input")

    def test_slow_approved_tool_is_not_human_wait_even_after_threshold(self):
        self.capture.return_value = ["• Working (45s • esc to interrupt)", "›"]
        self.post_state("waiting", kind="codex")
        self.timers[0].fire()
        self.line(self.wait(), "nothing yet")
        self.assertEqual(server.get_agent_state(self.project, "TASK-2"), "working")
        # A review can surface a real human prompt later; keep observing
        # until activity cancels it or there is positive evidence of input.
        self.capture.return_value = ["Press enter to confirm or esc to cancel"]
        self.timers[1].fire()
        cursor = self.line(self.wait(), "TASK-2 waiting for input")
        self.assertEqual(server.get_agent_state(self.project, "TASK-2"), "waiting")
        self.timers[1].fire()
        self.line(self.wait(after=cursor), "nothing yet")

    def test_activity_during_confirmation_capture_cancels_publication(self):
        self.post_state("waiting", kind="codex")
        def capture(*args):
            server.record_agent_event(self.project, "TASK-2", "working")
            return ["Press enter to confirm or esc to cancel"]
        self.capture.side_effect = capture
        self.timers[0].fire()
        self.line(self.wait(), "nothing yet")
        self.assertEqual(server.get_agent_state(self.project, "TASK-2"), "working")

    def test_ended_session_cannot_be_reported_waiting(self):
        self.capture.side_effect = server.PaneCaptureError("no session", status=404)
        self.post_state("waiting", kind="codex")
        self.timers[0].fire()
        self.line(self.wait(), "nothing yet")
        self.assertEqual(server.get_agent_state(self.project, "TASK-2"), "unknown")

    def test_failed_capture_is_unknown_and_retry_can_confirm_human_input(self):
        self.capture.side_effect = server.PaneCaptureError("tmux failed", status=500)
        self.post_state("waiting", kind="codex")
        self.timers[0].fire()
        self.line(self.wait(), "nothing yet")
        self.assertEqual(server.get_agent_state(self.project, "TASK-2"), "unknown")
        self.capture.side_effect = None
        self.timers[1].fire()
        self.line(self.wait(), "TASK-2 waiting for input")

    def test_stop_clear_reset_and_kind_change_cancel_pending_wait(self):
        for action, expected in (("stop", "idle"), ("clear", "unknown"),
                                 ("reset", "unknown"), ("kind", "finished")):
            with self.subTest(action=action):
                self.post_state("waiting", kind="codex")
                self.assertEqual(server.get_agent_state(self.project, "TASK-2"), "working")
                pending = self.timers[-1]
                if action == "stop":
                    server.record_agent_event(self.project, "TASK-2", "finished")
                    self.confirm_stop()
                elif action == "clear":
                    server.clear_agent_event(self.project, "task-2")
                elif action == "reset":
                    server._reset_agent_events()
                else:
                    self.post_state("finished", kind="claude")
                pending.fire()
                self.assertTrue(pending.cancelled)
                self.assertEqual(server.get_agent_state(self.project, "TASK-2"), expected)
        cursor = None
        for expected in ("TASK-2 idle (turn ended, may need input)", "TASK-2 finished (ready to review)"):
            cursor = self.line(self.wait(after=cursor), expected)
        self.line(self.wait(after=cursor), "nothing yet")

    def test_pending_waits_are_independent_and_clear_is_per_task(self):
        self.post_state("waiting", kind="codex", task="task-2")
        self.post_state("waiting", kind="codex", task="TASK-3")
        self.post_state("waiting", kind="codex", project=self.other)
        self.assertEqual(server.get_agent_state(self.project, "TASK-2"), "working")
        server.clear_agent_event(self.project, "TASK-2")
        for timer in self.timers:
            timer.fire()
        self.line(self.wait(), "TASK-3 waiting for input")
        self.line(self.wait(project=self.other), "TASK-2 waiting for input")
        self.assertEqual(server.get_agent_state(self.project, "TASK-2"), "unknown")

    def test_persistent_wait_wakes_an_already_blocked_http_request(self):
        import orchestrator
        entered = threading.Event()
        real_wait = orchestrator._condition.wait
        def observed_wait(timeout=None):
            entered.set()
            return real_wait(timeout)
        self.post_state("waiting", kind="codex")
        self.line(self.wait(), "nothing yet")
        with mock.patch.object(orchestrator._condition, "wait", side_effect=observed_wait):
            with concurrent.futures.ThreadPoolExecutor() as pool:
                result = pool.submit(self.wait, None, 2)
                self.assertTrue(entered.wait(1))
                self.assertFalse(result.done())
                self.timers[0].fire()
                self.line(result.result(timeout=2), "TASK-2 waiting for input")

    def test_harvest_history_survives_ui_log_eviction_and_names_failed_gate(self):
        for n in range(60):
            harvest._record_event(self.project, {"taskId": f"TASK-{n}", "merged": True}, "auto")
        harvest._record_event(self.project, {"taskId": "TASK-99", "gates": [
            {"name": "noLiveSession", "passed": True},
            {"name": "checkCommand", "passed": False, "reason": "tests failed\nexit 1"},
        ]}, "click")
        cursor = None
        for n in range(60):
            cursor = self.line(self.wait(after=cursor), f"TASK-{n} merged")
        self.line(self.wait(after=cursor), "TASK-99 merge blocked: checkCommand: tests failed exit 1")

    def test_harvest_errors_and_already_merged_are_reported(self):
        harvest._record_event(self.project, {"taskId": "TASK-2"}, "click", error="merge: command failed")
        cursor = self.line(self.wait(), "TASK-2 merge blocked: merge: command failed")
        harvest._record_event(self.project, {"taskId": "TASK-2", "alreadyMerged": True}, "click")
        self.line(self.wait(after=cursor), "TASK-2 merged (already merged)")

    def test_auto_harvest_repeated_live_session_block_publishes_once(self):
        config = {"projects": [{"name": self.project, "path": os.path.dirname(__file__)}],
                  "harvest": {"mode": "auto"}}
        session = f"centrale-{self.project}-task-2"

        def git(args, cwd=None):
            self.assertEqual(cwd, os.path.dirname(__file__))
            if args == ["for-each-ref", "--format=%(refname:short)", "refs/heads/task"]:
                return subprocess.CompletedProcess(args, 0, "task/task-2\n", "")
            self.assertEqual(args, ["rev-parse", "--verify", "--quiet", "refs/heads/task/task-2"])
            return subprocess.CompletedProcess(args, 0, "abc123\n", "")

        with mock.patch.object(server, "run_git", side_effect=git), \
             mock.patch.object(server, "list_sessions", return_value=[{"name": session}]):
            cycle = harvest.AutoHarvestThread(config)
            for _ in range(4):
                cycle.run_cycle()
        cursor = self.line(self.wait(),
                           f"TASK-2 merge blocked: noLiveSession: a live tmux session is still running: {session}")
        self.line(self.wait(after=cursor), "nothing yet")
        # The dashboard's attempt log still records every real attempt.
        self.assertEqual(len(harvest.recent_events(self.project)), 4)

    def test_three_already_merged_auto_cycles_publish_once(self):
        self.assert_repeated_auto_outcome_publishes_once(
            {"taskId": "TASK-2", "merged": False, "alreadyMerged": True},
            "TASK-2 merged (already merged)",
        )

    def test_three_identical_error_auto_cycles_publish_once(self):
        self.assert_repeated_auto_outcome_publishes_once(
            harvest.HarvestError("merge command failed", status=500),
            "TASK-2 merge blocked: merge command failed",
        )

    def assert_repeated_auto_outcome_publishes_once(self, outcome, expected):
        config = {"projects": [{"name": self.project, "path": os.path.dirname(__file__)}],
                  "harvest": {"mode": "auto"}}

        def git(args, cwd=None):
            self.assertEqual(args, ["for-each-ref", "--format=%(refname:short)", "refs/heads/task"])
            self.assertEqual(cwd, os.path.dirname(__file__))
            return subprocess.CompletedProcess(args, 0, "task/task-2\n", "")

        # Supply the gate/merge outcome; keep the real auto-cycle, harvest
        # wrapper (including its error path), event store and HTTP delivery.
        kwargs = {"side_effect": outcome} if isinstance(outcome, Exception) else {"return_value": outcome}
        with mock.patch.object(server, "run_git", side_effect=git), \
             mock.patch.object(harvest, "_harvest_branch_locked", **kwargs):
            cycle = harvest.AutoHarvestThread(config)
            for _ in range(3):
                cycle.run_cycle()
        attempts = harvest.recent_events(self.project)
        self.assertEqual(len(attempts), 3)
        self.assertTrue(all(event["trigger"] == "auto" for event in attempts))
        cursor = self.line(self.wait(), expected)
        self.assertEqual(self.line(self.wait(after=cursor), "nothing yet"), cursor)

    def test_harvest_changed_gate_or_reason_and_returning_block_are_published(self):
        cursor = None
        for gate, reason in [("noLiveSession", "busy"), ("taskDone", "not done"),
                             ("taskDone", "unchecked criteria"), ("noLiveSession", "busy")]:
            harvest._record_event(self.project, {"taskId": "TASK-2", "gates": [
                {"name": gate, "passed": False, "reason": reason},
            ]}, "auto")
            cursor = self.line(self.wait(after=cursor), f"TASK-2 merge blocked: {gate}: {reason}")
        self.line(self.wait(after=cursor), "nothing yet")

    def test_harvest_deduplication_is_per_project_and_task_and_ignores_hooks(self):
        report = {"taskId": "TASK-2", "gates": [
            {"name": "taskDone", "passed": False, "reason": "not done"},
        ]}
        harvest._record_event(self.project, report, "auto")
        cursor = self.line(self.wait(), "TASK-2 merge blocked: taskDone: not done")
        harvest._record_event(self.other, report, "auto")
        self.line(self.wait(project=self.other), "TASK-2 merge blocked: taskDone: not done")
        harvest._record_event(self.project, {**report, "taskId": "TASK-3"}, "auto")
        cursor = self.line(self.wait(after=cursor), "TASK-3 merge blocked: taskDone: not done")
        self.post_state("waiting")
        cursor = self.line(self.wait(after=cursor), "TASK-2 waiting for input")
        # Same rendered line, even with different raw whitespace, task case
        # and trigger. Neither a hook nor another task resets this history.
        harvest._record_event(self.project, {"taskId": "task-2", "gates": [
            {"name": "taskDone", "passed": False, "reason": "not\ndone"},
        ]}, "click")
        self.line(self.wait(after=cursor), "nothing yet")

    def test_harvest_changed_outcomes_reset_last_line_and_real_merges_always_publish(self):
        blocked = {"taskId": "TASK-2", "gates": [
            {"name": "taskDone", "passed": False, "reason": "not done"},
        ]}
        cursor = None
        for report, error, expected in [
            ({"taskId": "TASK-2", "merged": True}, None, "TASK-2 merged"),
            ({"taskId": "TASK-2", "alreadyMerged": True}, None, "TASK-2 merged (already merged)"),
            ({"taskId": "TASK-2"}, "merge failed", "TASK-2 merge blocked: merge failed"),
        ]:
            for _ in range(2 if report.get("merged") else 1):
                harvest._record_event(self.project, report, "auto", error=error)
                cursor = self.line(self.wait(after=cursor), expected)
            harvest._record_event(self.project, blocked, "auto")
            cursor = self.line(self.wait(after=cursor), "TASK-2 merge blocked: taskDone: not done")
        self.line(self.wait(after=cursor), "nothing yet")

    def test_projects_do_not_consume_each_others_events_or_cursors(self):
        self.post_state("waiting", project=self.other)
        cursor = self.line(self.wait(), "nothing yet")
        self.line(self.wait(project=self.other), "TASK-2 waiting for input")
        self.assertEqual(self.wait(project=self.other, after=cursor)[0], 409)

    def test_wait_wakes_on_hook_while_other_http_requests_remain_responsive(self):
        self.assert_hook_wakes("claude", "TASK-2 finished (ready to review)")

    def test_codex_idle_wakes_blocked_request(self):
        self.assert_hook_wakes("codex", "TASK-2 idle (turn ended, may need input)")

    def assert_hook_wakes(self, kind, expected):
        cursor = self.line(self.wait(), "nothing yet")
        # Observing Condition.wait avoids racing the request against the POST.
        import orchestrator
        entered = threading.Event()
        real_wait = orchestrator._condition.wait
        def observed_wait(timeout=None):
            entered.set()
            return real_wait(timeout)
        with mock.patch.object(orchestrator._condition, "wait", side_effect=observed_wait):
            with concurrent.futures.ThreadPoolExecutor() as pool:
                result = pool.submit(self.wait, cursor, 2)
                self.assertTrue(entered.wait(1), "request never entered its wait")
                self.assertFalse(result.done())
                self.assertEqual(self.request("/api/harvest-progress")[0], 200)
                self.post_state("finished", kind=kind)
                if kind == "codex":
                    self.confirm_stop()
                self.line(result.result(timeout=2), expected)

    def test_timeout_is_bounded_and_keeps_the_cursor(self):
        cursor = self.line(self.wait(), "nothing yet")
        start = time.monotonic()
        answer = self.wait(after=cursor, timeout=0.05)
        elapsed = time.monotonic() - start
        self.assertGreaterEqual(elapsed, 0.04)
        self.assertLess(elapsed, 1)
        self.assertEqual(self.line(answer, "nothing yet"), cursor)

    def test_invalid_requests_refuse_before_waiting(self):
        for query, status in [
            ("", 400), ("project=missing", 404),
            (f"project={self.project}&timeout=-1", 400),
            (f"project={self.project}&timeout=301", 400),
            (f"project={self.project}&timeout=nan", 400),
            (f"project={self.project}&timeout=inf", 400),
            (f"project={self.project}&timeout=no", 400),
            (f"project={self.project}&timeout=", 400),
            (f"project={self.project}&after=bad", 400),
            (f"project={self.project}&after=", 400),
        ]:
            with self.subTest(query=query):
                self.assertEqual(self.request("/api/orchestrator-wait?" + query)[0], status)

    def test_restart_and_future_cursors_are_explicit_conflicts(self):
        cursor = self.line(self.wait(), "nothing yet")
        stream = cursor.split(":")[0]
        for invalid in (stream + ":1", "0" * 32 + ":0"):
            status, body, _ = self.wait(after=invalid)
            self.assertEqual(status, 409)
            self.assertIn("cursor", json.loads(body)["error"])

    def test_wait_uses_existing_http_trust_boundary(self):
        path = f"/api/orchestrator-wait?project={self.project}&timeout=0"
        for headers in ({"Host": "evil.example"}, {"Sec-Fetch-Site": "cross-site"},
                        {"Origin": "https://evil.example"}):
            with self.subTest(headers=headers):
                self.assertEqual(self.request(path, headers=headers)[0], 403)
