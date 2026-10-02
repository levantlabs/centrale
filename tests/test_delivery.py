"""POST /api/deliver and GET /api/deliveries (task-170.2): deliver a line
to a spawned agent, confirm it by its echo on the pane, and log every
attempt. Hermetic: run_tmux is faked, the echo poll's clock and sleep are
injected, and the delivery log lives in a per-test temp dir."""

import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server  # noqa: E402

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "panes")
SESSION = "centrale-my-app-task-9"
TARGET = f"={SESSION}:"
SHORT = "centrale probe 1702: reply with only the word pong"
QUEUED = "centrale probe 176: queued delivery fixture; continue TASK-176."
LONG = (
    "centrale probe 1702 long: this is a deliberately long message that should wrap "
    "across several lines of a one hundred column pane so that the echo check can be "
    "measured against real word wrapping; reply with only the word pong"
)


def pane(name):
    """Real captures (claude 2.1.283 / codex 0.157.1), paths sanitised.

    Original fixtures: 100x30. Codex working queue: queue/footer excerpt
    of the 220x200 TASK-176 worker, captured 2026-09-27 with capture-pane -p.
    """
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as f:
        return f.read().split("\n")


class DialogDetectionTests(unittest.TestCase):
    def test_resume_picker_and_working_directory_dialog_refuse_delivery(self):
        for lines in (["Resume a previous session", "Updated  Branch  Conversation", "› task B"],
                      ["Working directory · resume", "Session = latest cwd recorded in the resumed session",
                       "1. Use session directory (/worktrees/b)", "2. Use current directory (/worktrees/a)"]):
            with self.subTest(lines=lines), \
                 mock.patch.object(server, "capture_session_pane", return_value=lines), \
                 mock.patch.object(server, "send_session_input") as send:
                result = server.deliver_message(SESSION, "[ruling from owner] Continue task A")
                self.assertEqual(result["outcome"], "dialog")
                send.assert_not_called()

    def test_loading_resume_refuses_input_before_its_dialog_can_appear(self):
        lines = ["Resuming session…", "› Ask Codex to do anything", "? for shortcuts"]
        with mock.patch.object(server, "capture_session_pane", return_value=lines), \
             mock.patch.object(server, "send_session_input") as send:
            result = server.deliver_message(SESSION, "[ruling from owner] Continue")
        self.assertEqual(result["outcome"], "dialog")
        send.assert_not_called()

    def test_loading_marker_only_in_scrollback_does_not_block_a_ruling(self):
        current = ["› Ask Codex to do anything", "? for shortcuts"]
        history = ["Resuming session…"] + [f"old row {i}" for i in range(20)] + current
        text = "[ruling from owner] Continue"
        with mock.patch.object(server, "capture_session_pane", side_effect=[history, current, ["› " + text]]) as capture, \
             mock.patch.object(server, "send_session_input") as send, \
             mock.patch.object(server, "_delivery_sleep"):
            result = server.deliver_message(SESSION, text)
        self.assertEqual(result["outcome"], "delivered")
        send.assert_called_once()
        self.assertIn(mock.call(SESSION, 0), capture.call_args_list)

    def test_historical_resume_heading_above_normal_composer_is_not_a_dialog(self):
        lines = ["Resume a previous session"] + [f"old transcript row {i}" for i in range(20)]
        lines += ["› Ask Codex to do anything", "? for shortcuts"]
        self.assertIsNone(server.detect_pane_dialog(lines))
        self.assertIsNone(server.detect_pane_dialog(["Resume session using an explicit UUID."]))

    def test_real_trust_dialogs_are_detected_by_their_footer(self):
        self.assertEqual(
            server.detect_pane_dialog(pane("claude-trust-dialog.txt")),
            "Enter to confirm · Esc to cancel",
        )
        self.assertEqual(
            server.detect_pane_dialog(pane("codex-trust-dialog.txt")),
            "enter continue · esc back",
        )

    def test_real_idle_panes_are_not_dialogs(self):
        # Including claude's ghost suggestion and both "esc to interrupt"
        # working footers' neighbours -- nothing here names Enter then Esc.
        self.assertIsNone(server.detect_pane_dialog(pane("claude-idle-after-reply.txt")))
        self.assertIsNone(server.detect_pane_dialog(pane("codex-idle-after-reply.txt")))

    def test_working_footers_are_not_dialogs(self):
        self.assertIsNone(server.detect_pane_dialog([
            "❯ do the thing", "✶ Kerfuffling…", "❯ ",
            "  ⏵⏵ auto mode on (shift+tab to cycle) · esc to interrupt",
        ]))
        self.assertIsNone(server.detect_pane_dialog([
            "› do the thing", "• Working (0s • esc to interrupt)", "› Ask Codex to do anything",
        ]))

    def test_cursor_on_a_numbered_menu_is_a_dialog(self):
        lines = [
            " Do you want to proceed?",
            " ❯ 1. Yes",
            "   2. Yes, and don't ask again for this command",
            "   3. No, and tell Claude what to do differently (esc)",
        ]
        self.assertEqual(server.detect_pane_dialog(lines), "❯ 1. Yes")

    def test_codex_approval_footer_is_a_dialog(self):
        self.assertEqual(
            server.detect_pane_dialog(["Allow command?", "Press enter to confirm or esc to cancel"]),
            "Press enter to confirm or esc to cancel",
        )

    def test_an_echoed_numbered_message_alone_is_not_a_menu(self):
        self.assertIsNone(server.detect_pane_dialog(["❯ 1. fix the parser", "● Done."]))

    def test_only_the_bottom_of_the_pane_counts(self):
        # An agent's earlier prose mentioning the keys, scrolled up above
        # the dialog region, is not a dialog.
        lines = ["Enter to confirm · Esc to cancel"] + [f"● line {i}" for i in range(20)]
        self.assertIsNone(server.detect_pane_dialog(lines))


class EchoMatchingTests(unittest.TestCase):
    def test_short_message_echo_on_real_panes(self):
        for name, glyph in (("claude-idle-after-reply.txt", "❯"), ("codex-idle-after-reply.txt", "›")):
            with self.subTest(pane=name):
                self.assertEqual(server.echo_lines(pane(name), SHORT), [f"{glyph} {SHORT}"])

    def test_wrapped_long_message_matches_its_first_row(self):
        for name in ("claude-idle-after-reply.txt", "codex-idle-after-reply.txt"):
            with self.subTest(pane=name):
                found = server.echo_lines(pane(name), LONG)
                self.assertEqual(len(found), 1)
                self.assertTrue(found[0].endswith("should wrap across several"))

    def test_real_codex_queue_is_an_echo(self):
        self.assertEqual(server.echo_lines(pane("codex-working-queued.txt"), QUEUED),
                         [f"↳ {QUEUED}"])

    def test_arrow_without_codex_queue_heading_is_not_an_echo(self):
        self.assertEqual(server.echo_lines([f"↳ {QUEUED}"], QUEUED), [])
        self.assertEqual(server.echo_lines([
            "• Messages to be submitted after next tool call (press esc to interrupt and send immediately)",
            "  ↳ another message", "", "• Agent reply", f"  ↳ {QUEUED}"], QUEUED), [])

    def test_wrapped_queued_message_matches_only_its_leading_row(self):
        lines = [
            "• Messages to be submitted after next tool call (press esc to interrupt and send immediately)",
            "  ↳ centrale probe 176: queued delivery fixture;",
            "    continue TASK-176.",
        ]
        self.assertEqual(server.echo_lines(lines, QUEUED), ["↳ centrale probe 176: queued delivery fixture;"])

    def test_agent_reply_quoting_the_text_is_not_an_echo(self):
        self.assertEqual(server.echo_lines(pane("claude-idle-after-reply.txt"), "pong"), [])

    def test_a_different_prompt_or_ghost_suggestion_is_not_an_echo(self):
        lines = ['❯ Try "how do I log an error?"', "❯ yes please do", "❯ "]
        self.assertEqual(server.echo_lines(lines, "yes"), [])
        self.assertEqual(server.echo_lines(lines, "yes   please do"), ["❯ yes please do"])


class FakeTmux:
    """run_tmux stand-in. capture-pane answers from `panes` -- one list of
    lines per capture, the last repeated -- or with `capture_error`;
    everything else succeeds unless `fail` names its subcommand."""

    def __init__(self, panes, capture_error=None, fail=None):
        self.panes = list(panes)
        self.capture_error = capture_error
        self.fail = fail or {}
        self.calls = []
        self.stdins = []

    def __call__(self, args, input=None):
        self.calls.append(list(args))
        self.stdins.append(input)
        if args[0] in self.fail:
            return subprocess.CompletedProcess(["tmux", *args], 1, "", self.fail[args[0]])
        if args[0] == "capture-pane":
            if self.capture_error is not None:
                return subprocess.CompletedProcess(["tmux", *args], 1, "", self.capture_error)
            lines = self.panes.pop(0) if len(self.panes) > 1 else self.panes[0]
            return subprocess.CompletedProcess(["tmux", *args], 0, "\n".join(lines) + "\n", "")
        return subprocess.CompletedProcess(["tmux", *args], 0, "", "")

    def kinds(self):
        return [c[0] for c in self.calls]


class ApiHarness(unittest.TestCase):
    """An in-process server with tmux, git and backlog faked, the echo
    poll's clock and sleep injected, and the delivery log in a temp dir --
    shared by the /api/deliver and /api/rule tests."""

    @classmethod
    def setUpClass(cls):
        here = os.path.dirname(os.path.abspath(__file__))
        cls.config = {"port": 0, "worktreeRoot": "/tmp/does-not-matter",
                      "projects": [{"name": "my-app", "path": here}]}
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
        server._reset_pane_capture_times()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.log_path = os.path.join(tmp.name, "state", "deliveries.jsonl")
        for patcher in (
            mock.patch.dict(os.environ, {server.DELIVERY_LOG_ENV: self.log_path}),
            # A spawned task's file is never written: no git, no backlog.
            mock.patch.object(server, "run_git", side_effect=AssertionError("no git")),
            mock.patch.object(server, "run_backlog", side_effect=AssertionError("no backlog")),
            mock.patch.object(server, "_new_paste_buffer_name", return_value="centrale-reply-test"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        # The echo poll: no real waiting, and a clock that moves one poll
        # interval per sleep, so the timeout is reached deterministically.
        self.now = [1000.0]
        self.sleeps = []

        def fake_sleep(seconds):
            self.sleeps.append(seconds)
            self.now[0] += seconds

        for patcher in (
            mock.patch.object(server, "_delivery_sleep", side_effect=fake_sleep),
            mock.patch.object(server, "_delivery_clock", side_effect=lambda: self.now[0]),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _request(self, method, path, payload=None):
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=data, method=method,
            headers={"Content-Type": "application/json"} if data is not None else {},
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def _log(self):
        if not os.path.exists(self.log_path):
            return []
        with open(self.log_path, encoding="utf-8") as f:
            return [json.loads(line) for line in f]


class DeliverApiTests(ApiHarness):
    def _deliver(self, tmux, **overrides):
        body = {"project": "my-app", "taskId": "TASK-9", "sender": "lead-agent", "text": SHORT}
        body.update(overrides)
        with mock.patch.object(server, "run_tmux", side_effect=tmux):
            return self._request("POST", "/api/deliver", body)

    def _idle_before(self):
        # The claude pane as it was before the probe: nothing echoed yet.
        return [line for line in pane("claude-idle-after-reply.txt") if "centrale probe" not in line
                and "wrapping;" not in line and "lines of a one" not in line]

    # -- delivered ---------------------------------------------------------

    def test_delivered_returns_the_echoed_line_and_a_timestamp(self):
        tmux = FakeTmux([self._idle_before(), self._idle_before(), pane("claude-idle-after-reply.txt")])
        status, body = self._deliver(tmux)
        self.assertEqual(status, 200)
        self.assertIs(body["ok"], True)
        self.assertEqual(body["outcome"], "delivered")
        self.assertEqual(body["echo"], f"❯ {SHORT}")
        self.assertRegex(body["echoAt"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assertEqual(body["session"], SESSION)
        self.assertIs(body["logged"], True)
        self.assertNotIn("error", body)
        # Capture, then the drawer's own bracketed-paste path (one paste
        # path, not two), then captures until the echo shows.
        self.assertEqual(tmux.calls[1:4], [
            ["load-buffer", "-b", "centrale-reply-test", "-"],
            ["paste-buffer", "-d", "-p", "-b", "centrale-reply-test", "-t", TARGET],
            ["send-keys", "-t", TARGET, "Enter"],
        ])
        self.assertEqual(tmux.stdins[1], SHORT)
        self.assertEqual(tmux.kinds(), ["capture-pane", "load-buffer", "paste-buffer", "send-keys",
                                        "capture-pane", "capture-pane"])
        self.assertEqual(self.sleeps, [server.DELIVERY_ECHO_POLL_SECONDS] * 2)

    def test_mid_turn_codex_queue_returns_delivered_and_logs_its_echo(self):
        tmux = FakeTmux([["• Working", "›"], pane("codex-working-queued.txt")])
        status, body = self._deliver(tmux, text=QUEUED)
        self.assertEqual(status, 200)
        self.assertEqual(body["outcome"], "delivered")
        self.assertEqual(body["echo"], f"↳ {QUEUED}")
        self.assertEqual(self._log()[0]["outcome"], "delivered")

    def test_existing_queue_or_queue_moving_into_transcript_is_not_new_delivery(self):
        for after in (pane("codex-working-queued.txt"), [f"› {QUEUED}"]):
            with self.subTest(after=after):
                tmux = FakeTmux([pane("codex-working-queued.txt"), after])
                status, body = self._deliver(tmux, text=QUEUED)
                self.assertEqual(status, 504)
                self.assertEqual(body["outcome"], "no-echo")

    def test_needs_no_prior_pane_capture_and_does_not_arm_the_drawer_reply(self):
        self.assertIsNone(server.pane_capture_age(SESSION))
        status, _ = self._deliver(FakeTmux([self._idle_before(), pane("claude-idle-after-reply.txt")]))
        self.assertEqual(status, 200)
        self.assertIsNone(server.pane_capture_age(SESSION))

    def test_session_preview_mode_does_not_gate_delivery(self):
        self.config["sessionPreview"] = {"mode": "off"}
        self.addCleanup(lambda: self.config.pop("sessionPreview", None))
        status, _ = self._deliver(FakeTmux([self._idle_before(), pane("claude-idle-after-reply.txt")]))
        self.assertEqual(status, 200)

    def test_every_attempt_is_logged_with_its_outcome(self):
        self._deliver(FakeTmux([self._idle_before(), pane("claude-idle-after-reply.txt")]))
        [entry] = self._log()
        self.assertEqual(
            {k: entry[k] for k in ("project", "task", "sender", "text", "session", "outcome", "echo")},
            {"project": "my-app", "task": "TASK-9", "sender": "lead-agent", "text": SHORT,
             "session": SESSION, "outcome": "delivered", "echo": f"❯ {SHORT}"},
        )
        self.assertRegex(entry["time"], r"Z$")
        self.assertRegex(entry["id"], r"^[0-9a-f]{32}$")

    # -- failures ----------------------------------------------------------

    def test_an_identical_earlier_echo_cannot_pass_for_this_one(self):
        # The pane already shows this exact message; after the send it
        # shows nothing new. The baseline count keeps that from reading
        # as delivered.
        tmux = FakeTmux([pane("claude-idle-after-reply.txt")])
        status, body = self._deliver(tmux)
        self.assertEqual(status, 504)
        self.assertIs(body["ok"], False)
        self.assertEqual(body["outcome"], "no-echo")
        self.assertIn("no echo", body["error"])
        self.assertIsNone(body["echo"])
        polls = int(server.DELIVERY_ECHO_TIMEOUT_SECONDS / server.DELIVERY_ECHO_POLL_SECONDS)
        self.assertEqual(len(self.sleeps), polls)
        self.assertEqual(self._log()[0]["outcome"], "no-echo")

    def test_a_dialog_on_the_pane_fails_explicitly_and_nothing_is_pasted(self):
        for fixture, line in (("claude-trust-dialog.txt", "Enter to confirm · Esc to cancel"),
                              ("codex-trust-dialog.txt", "enter continue · esc back")):
            with self.subTest(pane=fixture):
                tmux = FakeTmux([pane(fixture)])
                status, body = self._deliver(tmux)
                self.assertEqual(status, 409)
                self.assertIs(body["ok"], False)
                self.assertEqual(body["outcome"], "dialog")
                self.assertEqual(body["dialog"], line)
                self.assertIn("nothing was sent", body["error"])
                self.assertEqual(tmux.kinds(), ["capture-pane"])
        self.assertEqual([e["outcome"] for e in self._log()], ["dialog", "dialog"])

    def test_a_dialog_that_appears_after_the_send_is_named(self):
        tmux = FakeTmux([self._idle_before(), pane("claude-trust-dialog.txt")])
        status, body = self._deliver(tmux)
        self.assertEqual(status, 409)
        self.assertEqual(body["outcome"], "dialog")
        self.assertIn("the text was sent", body["error"])

    def test_no_such_session(self):
        tmux = FakeTmux([], capture_error=f"can't find session: {SESSION}")
        status, body = self._deliver(tmux)
        self.assertEqual(status, 404)
        self.assertEqual(body["outcome"], "no-session")
        self.assertEqual(tmux.kinds(), ["capture-pane"])
        self.assertEqual(self._log()[0]["outcome"], "no-session")

    def test_session_gone_between_capture_and_paste(self):
        tmux = FakeTmux([self._idle_before()], fail={"paste-buffer": "can't find session"})
        status, body = self._deliver(tmux)
        self.assertEqual(status, 404)
        self.assertEqual(body["outcome"], "no-session")

    def test_other_tmux_failure_is_a_500(self):
        tmux = FakeTmux([self._idle_before()], fail={"send-keys": "server exited unexpectedly"})
        status, body = self._deliver(tmux)
        self.assertEqual(status, 500)
        self.assertEqual(body["outcome"], "tmux-error")
        self.assertIn("send-keys", body["error"])

    def test_log_write_failure_is_reported_not_hidden(self):
        os.makedirs(self.log_path)  # a directory where the file should be
        status, body = self._deliver(FakeTmux([self._idle_before(), pane("claude-idle-after-reply.txt")]))
        self.assertEqual(status, 200)
        self.assertIs(body["logged"], False)
        self.assertIn("delivery log", body["logError"])

    # -- not an attempt: rejected before tmux, not logged ------------------

    def test_invalid_requests_touch_no_tmux_and_log_nothing(self):
        refuse = mock.Mock(side_effect=AssertionError("tmux must not run"))
        cases = [
            ({"sender": None}, 400, "sender"),
            ({"sender": ""}, 400, "sender"),
            ({"sender": "x" * 101}, 400, "sender"),
            ({"sender": "a\nb"}, 400, "sender"),
            ({"text": "two\nlines"}, 400, "single line"),
            ({"text": ""}, 400, "empty"),
            ({"key": "Escape"}, 400, "unknown"),
            ({"taskId": "nope!"}, 400, "task id"),
            ({"project": "nope"}, 404, "unknown project"),
        ]
        for overrides, status_expected, needle in cases:
            with self.subTest(overrides=overrides):
                body = {"project": "my-app", "taskId": "TASK-9", "sender": "cto", "text": "hi"}
                body.update(overrides)
                body = {k: v for k, v in body.items() if v is not None}
                with mock.patch.object(server, "run_tmux", refuse):
                    status, resp = self._request("POST", "/api/deliver", body)
                self.assertEqual(status, status_expected)
                self.assertIn(needle, resp["error"])
        self.assertEqual(self._log(), [])

    # -- GET /api/deliveries ----------------------------------------------

    def test_log_is_readable_via_the_api_filtered_and_limited(self):
        status, body = self._request("GET", "/api/deliveries")
        self.assertEqual((status, body["deliveries"], body["log"]), (200, [], self.log_path))

        self._deliver(FakeTmux([self._idle_before(), pane("claude-idle-after-reply.txt")]))
        self._deliver(FakeTmux([pane("claude-trust-dialog.txt")]), taskId="TASK-10")
        self._deliver(FakeTmux([pane("claude-trust-dialog.txt")]), taskId="TASK-10", text="second")
        with open(self.log_path, "a", encoding="utf-8") as f:
            f.write("not json\n")

        status, body = self._request("GET", "/api/deliveries")
        self.assertEqual(status, 200)
        self.assertEqual([e["task"] for e in body["deliveries"]], ["TASK-9", "TASK-10", "TASK-10"])
        self.assertEqual(body["skippedLines"], 1)

        _, body = self._request("GET", "/api/deliveries?project=my-app&task=task-10")
        self.assertEqual([e["text"] for e in body["deliveries"]], [SHORT, "second"])
        _, body = self._request("GET", "/api/deliveries?project=my-app&task=TASK-10&limit=1")
        self.assertEqual([e["text"] for e in body["deliveries"]], ["second"])
        _, body = self._request("GET", "/api/deliveries?project=other")
        self.assertEqual(body["deliveries"], [])

    def test_deliveries_rejects_a_bad_limit_or_task(self):
        for query in ("limit=0", "limit=1001", "limit=x", "task=bad!"):
            with self.subTest(query=query):
                status, _ = self._request("GET", f"/api/deliveries?{query}")
                self.assertEqual(status, 400)


class RuleApiTests(ApiHarness):
    """POST /api/rule (task-172): one call rules on a spawned task. A live
    agent gets the ruling through /api/deliver's own path; with none, it
    is committed on the task branch by spawn.commit_ruling. The session
    list, the delivery and the commit are faked at their seams here; the
    commit's real git/backlog behaviour is in
    tests_integration/test_task_lock_integration.py."""

    RULING = "keep the old flag; deprecate it next release"

    def setUp(self):
        super().setUp()
        import spawn

        self.spawn = spawn
        self.commit = mock.MagicMock(return_value={
            "branch": "task/task-9", "worktree": "/wt", "path": "backlog/tasks/task-9 - X.md",
            "commit": "abc123",
        })
        patcher = mock.patch.object(spawn, "commit_ruling", self.commit)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _rule(self, sessions, deliver=None, **overrides):
        body = {"project": "my-app", "taskId": "TASK-9", "sender": "lead-agent", "text": self.RULING}
        body.update(overrides)
        listing = mock.patch.object(server, "list_sessions", side_effect=sessions)
        delivery = mock.patch.object(
            server, "deliver_message",
            side_effect=deliver or AssertionError("nothing should be delivered"))
        with listing, delivery as delivered:
            status, payload = self._request("POST", "/api/rule", body)
        return status, payload, delivered

    def test_a_live_agent_gets_the_ruling_with_its_sender_and_the_confirmation_comes_back(self):
        live = [{"name": SESSION}]
        status, body, delivered = self._rule(
            lambda: live,
            deliver=lambda name, text: {"outcome": "delivered", "echo": f"❯ {text}", "echoAt": "t"})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["mode"], "delivered")
        self.assertIs(body["ok"], True)
        expected = f"[ruling from lead-agent] {self.RULING}"
        delivered.assert_called_once_with(SESSION, expected)
        self.assertEqual(body["echo"], f"❯ {expected}")
        self.commit.assert_not_called()
        # The delivery log is the record of what was sent.
        [entry] = self._log()
        self.assertEqual((entry["sender"], entry["text"], entry["outcome"]),
                         ("lead-agent", expected, "delivered"))

    def test_a_failed_delivery_to_a_live_agent_is_returned_and_nothing_is_written_around_it(self):
        status, body, _ = self._rule(
            lambda: [{"name": SESSION}],
            deliver=lambda name, text: {"outcome": "dialog", "dialog": "Enter to confirm · Esc to cancel",
                                        "reason": "a dialog is occupying the pane"})
        self.assertEqual(status, 409, body)
        self.assertEqual(body["mode"], "delivered")
        self.assertEqual(body["outcome"], "dialog")
        self.commit.assert_not_called()

    def test_rule_refuses_codex_resume_picker_before_any_input(self):
        with mock.patch.object(server, "list_sessions", return_value=[{"name": SESSION}]), \
             mock.patch.object(server, "capture_session_pane", return_value=["Resume a previous session", "› other task"]), \
             mock.patch.object(server, "send_session_input") as send:
            status, body = self._request("POST", "/api/rule", {
                "project": "my-app", "taskId": "TASK-9", "sender": "lead-agent", "text": self.RULING,
            })
        self.assertEqual(status, 409)
        self.assertEqual(body["outcome"], "dialog")
        self.assertFalse(body["ok"])
        send.assert_not_called()
        self.commit.assert_not_called()

    def test_no_live_agent_commits_the_ruling_on_the_branch(self):
        status, body, _ = self._rule(lambda: [])
        self.assertEqual(status, 200, body)
        self.assertEqual(body["mode"], "committed")
        self.assertEqual(body["commit"], "abc123")
        args = self.commit.call_args.args
        self.assertEqual((args[1]["name"], args[2], args[3], args[4]),
                         ("my-app", "TASK-9", "lead-agent", self.RULING))
        self.assertEqual(self._log(), [])  # nothing was delivered, so nothing is logged

    def test_a_session_that_ends_before_the_echo_falls_back_to_the_commit(self):
        calls = iter([[{"name": SESSION}], []])
        status, body, delivered = self._rule(
            lambda: next(calls),
            deliver=lambda name, text: {"outcome": "no-session", "reason": "gone"})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["mode"], "committed")
        delivered.assert_called_once()
        self.commit.assert_called_once()

    def test_a_session_that_starts_while_the_ruling_is_written_is_a_409(self):
        calls = iter([[], [{"name": SESSION}]])
        status, body, _ = self._rule(lambda: next(calls))
        self.assertEqual(status, 409, body)
        self.assertIn("send it again", body["error"])
        self.commit.assert_not_called()

    def test_an_unreadable_session_list_is_a_502_not_no_agent(self):
        def broken():
            raise server.BacklogError("tmux list-sessions failed: boom")
        status, body, _ = self._rule(broken)
        self.assertEqual(status, 502, body)
        self.commit.assert_not_called()

    def test_commit_ruling_refusals_pass_through(self):
        self.commit.side_effect = self.spawn.SpawnError("TASK-9 is not spawned: ...", status=409)
        status, body, _ = self._rule(lambda: [])
        self.assertEqual(status, 409)
        self.assertIn("not spawned", body["error"])

    def test_validation_is_deliver_s_and_the_prefixed_line_must_fit(self):
        for overrides in ({"sender": ""}, {"text": "two\nlines"}, {"taskId": "bad!"},
                          {"extra": 1}, {"text": "x" * 990}):
            with self.subTest(overrides=overrides):
                status, _, _ = self._rule(lambda: [], **overrides)
                self.assertEqual(status, 400)
        status, _, _ = self._rule(lambda: [], project="nope")
        self.assertEqual(status, 404)
        self.commit.assert_not_called()


class DeliveryLogPathTests(unittest.TestCase):
    def test_env_override_then_xdg_state_home_then_home(self):
        with mock.patch.dict(os.environ, {server.DELIVERY_LOG_ENV: "/x/log.jsonl"}):
            self.assertEqual(server.delivery_log_path(), "/x/log.jsonl")
        env = {k: v for k, v in os.environ.items() if k != server.DELIVERY_LOG_ENV}
        env["XDG_STATE_HOME"] = "/state"
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(server.delivery_log_path(), "/state/centrale/deliveries.jsonl")
        env.pop("XDG_STATE_HOME")
        env["HOME"] = "/home/user"
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(server.delivery_log_path(),
                             "/home/user/.local/state/centrale/deliveries.jsonl")


if __name__ == "__main__":
    unittest.main()
