import io
import http.server
import json
import os
import sys
import socket
import subprocess
import threading
import time
import unittest
import urllib.error
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import centrale_notify  # noqa: E402


class MainArgvHandlingTests(unittest.TestCase):
    """main()'s own argv/env gating, with _post_event mocked out -- the
    actual POST is covered separately below against a real local HTTP
    server, never a live agent or external endpoint."""

    def test_missing_state_argument_is_a_noop(self):
        with mock.patch.object(centrale_notify, "_post_event") as post_event:
            centrale_notify.main(["centrale_notify.py"])
        post_event.assert_not_called()

    def test_missing_event_url_is_a_noop(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CENTRALE_EVENT_URL", None)
            with mock.patch.object(centrale_notify, "_post_event") as post_event:
                centrale_notify.main(["centrale_notify.py", "working"])
        post_event.assert_not_called()

    def test_posts_the_given_state_to_centrale_event_url(self):
        with mock.patch.dict(os.environ, {"CENTRALE_EVENT_URL": "http://127.0.0.1:9/x"}):
            with mock.patch.object(centrale_notify, "_post_event") as post_event:
                centrale_notify.main(["centrale_notify.py", "working"])
        post_event.assert_called_once_with("http://127.0.0.1:9/x", "working", mock.ANY)

    def test_extra_argv_is_ignored(self):
        # codex always appends one extra JSON-payload argument describing
        # the completed turn -- must not change which state gets posted,
        # or trip up on it at all.
        with mock.patch.dict(os.environ, {"CENTRALE_EVENT_URL": "http://127.0.0.1:9/x"}):
            with mock.patch.object(centrale_notify, "_post_event") as post_event:
                centrale_notify.main(["centrale_notify.py", "finished", '{"turn": "payload"}'])
        post_event.assert_called_once_with("http://127.0.0.1:9/x", "finished", mock.ANY)

    def test_notification_payload_distinguishes_real_prompts_from_reminders(self):
        for notification_type in ("permission_prompt", "elicitation_dialog",
                                  "elicitation_url_dialog", "agent_needs_input",
                                  "idle_prompt", "auth_success", "agent_completed", "future_type"):
            with self.subTest(notification_type=notification_type), \
                 mock.patch.dict(os.environ, {"CENTRALE_EVENT_URL": "http://127.0.0.1:9/x"}), \
                 mock.patch.object(sys, "stdin", io.StringIO(json.dumps({"notification_type": notification_type}))), \
                 mock.patch.object(centrale_notify, "_post_event") as post:
                centrale_notify.main(["notify", "notification"])
                if notification_type in {"permission_prompt", "elicitation_dialog", "elicitation_url_dialog", "agent_needs_input"}:
                    post.assert_called_once_with("http://127.0.0.1:9/x", "waiting", mock.ANY)
                else:
                    post.assert_not_called()

    def test_bad_notification_payloads_are_silent_and_publish_nothing(self):
        for payload in ("", "broken", "[]", "null", "{}"):
            with self.subTest(payload=payload), \
                 mock.patch.dict(os.environ, {"CENTRALE_EVENT_URL": "http://127.0.0.1:9/x"}), \
                 mock.patch.object(sys, "stdin", io.StringIO(payload)), \
                 mock.patch.object(centrale_notify, "_post_event") as post:
                centrale_notify.main(["notify", "notification"])
                post.assert_not_called()

    def test_post_event_failure_is_swallowed(self):
        # A hook must never be able to break or hang the agent it's
        # attached to -- any exception from the actual network call is
        # caught inside main(), never propagated.
        with mock.patch.dict(os.environ, {"CENTRALE_EVENT_URL": "http://127.0.0.1:9/x"}):
            with mock.patch.object(centrale_notify, "_post_event", side_effect=OSError("boom")), \
                 mock.patch.object(centrale_notify, "_retry_in_background",
                                   side_effect=OSError("fork failed")):
                centrale_notify.main(["centrale_notify.py", "working"])  # must not raise

    def test_unreachable_server_hands_the_same_event_to_the_background_retrier(self):
        # task-201: the hook itself never waits for a restarting server.
        with mock.patch.dict(os.environ, {"CENTRALE_EVENT_URL": "http://127.0.0.1:9/x"}), \
             mock.patch.object(centrale_notify, "_post_event", side_effect=OSError("refused")) as post, \
             mock.patch.object(centrale_notify, "_retry_in_background") as retry:
            centrale_notify.main(["centrale_notify.py", "finished"])
        fired_at = post.call_args.args[2]
        retry.assert_called_once_with("http://127.0.0.1:9/x", "finished", fired_at)

    def test_an_answered_error_is_not_retried(self):
        error = urllib.error.HTTPError("http://127.0.0.1:9/x", 400, "bad", {}, None)
        with mock.patch.dict(os.environ, {"CENTRALE_EVENT_URL": "http://127.0.0.1:9/x"}), \
             mock.patch.object(centrale_notify, "_post_event", side_effect=error), \
             mock.patch.object(centrale_notify, "_retry_in_background") as retry:
            centrale_notify.main(["centrale_notify.py", "finished"])
        retry.assert_not_called()


class RetryUntilDeliveredTests(unittest.TestCase):
    def test_retries_until_the_server_answers_then_stops(self):
        outcomes = [OSError("refused"), OSError("refused"), None]
        with mock.patch.object(centrale_notify, "_post_event", side_effect=outcomes) as post, \
             mock.patch.object(centrale_notify, "RETRY_INTERVAL_SECONDS", 0.01):
            centrale_notify._retry_until_delivered("http://u", "finished", 12.5)
        self.assertEqual(post.call_count, 3)
        self.assertEqual({c.args[:3] for c in post.call_args_list}, {("http://u", "finished", 12.5)})

    def test_gives_up_within_the_bounded_window(self):
        with mock.patch.object(centrale_notify, "_post_event", side_effect=OSError("refused")), \
             mock.patch.object(centrale_notify, "RETRY_SECONDS", 0.2), \
             mock.patch.object(centrale_notify, "RETRY_INTERVAL_SECONDS", 0.02):
            started = time.monotonic()
            centrale_notify._retry_until_delivered("http://u", "finished", 1.0)
        self.assertLess(time.monotonic() - started, 1.0)


class RecordingHandler(http.server.BaseHTTPRequestHandler):
    """Records every request body it receives, then always answers 200 --
    a real (localhost-only, ephemeral-port) HTTP server standing in for
    the centrale server's own /api/agent-event, so _post_event's actual
    request shape is checked against something real rather than a mock."""

    received = []

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        RecordingHandler.received.append({
            "path": self.path,
            "headers": dict(self.headers),
            "body": json.loads(body.decode("utf-8")) if body else None,
        })
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, fmt, *args):
        pass


class PostEventTests(unittest.TestCase):
    """_post_event against a real ThreadingHTTPServer bound to
    127.0.0.1:0 -- never a real agent, external network, or the actual
    centrale server."""

    @classmethod
    def setUpClass(cls):
        RecordingHandler.received = []
        cls.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), RecordingHandler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join(timeout=5)

    def setUp(self):
        RecordingHandler.received = []

    def _url(self):
        return f"http://127.0.0.1:{self.port}/api/agent-event?project=my-app&task=TASK-2"

    def test_posts_json_body_with_the_state(self):
        centrale_notify._post_event(self._url(), "working", 1234.5)

        self.assertEqual(len(RecordingHandler.received), 1)
        received = RecordingHandler.received[0]
        self.assertEqual(received["body"], {"state": "working", "firedAt": 1234.5})
        self.assertEqual(received["headers"]["Content-Type"], "application/json")

    def test_preserves_the_query_string_carrying_project_and_task(self):
        centrale_notify._post_event(self._url(), "finished", 1.0)
        self.assertEqual(
            RecordingHandler.received[0]["path"],
            "/api/agent-event?project=my-app&task=TASK-2",
        )

    def test_main_end_to_end_against_the_real_local_server(self):
        with mock.patch.dict(os.environ, {"CENTRALE_EVENT_URL": self._url()}):
            before = time.time()
            centrale_notify.main(["centrale_notify.py", "waiting"])
        body = RecordingHandler.received[0]["body"]
        self.assertEqual(body["state"], "waiting")
        self.assertTrue(before <= body["firedAt"] <= time.time())

    def test_connection_failure_raises_from_post_event(self):
        # A real connection failure (nothing listening on this port) --
        # _post_event itself is allowed to raise (main() is the one that
        # swallows it, see MainArgvHandlingTests); this confirms it
        # really is a plain, promptly-raised urllib exception, not
        # something that hangs until the 2s timeout.
        with self.assertRaises(Exception):
            centrale_notify._post_event("http://127.0.0.1:1/nope", "working", 1.0)


class DownServerRetryTests(unittest.TestCase):
    """task-201 AC #4: the real script, run as its own process the way a
    hook runs it, against a port nothing listens on until shortly after
    the hook has already returned. Local loopback only."""

    def test_event_fired_while_down_lands_once_the_server_is_back(self):
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        url = f"http://127.0.0.1:{port}/api/agent-event?project=p&task=TASK-1"
        script = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                              "centrale_notify.py")
        started = time.monotonic()
        proc = subprocess.run([sys.executable, script, "finished"], timeout=10,
                              env={**os.environ, "CENTRALE_EVENT_URL": url},
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        # The hook (whose stdout pipe the runner waits on) returns at once.
        self.assertLess(time.monotonic() - started, 1.5)
        self.assertEqual((proc.returncode, proc.stdout, proc.stderr), (0, b"", b""))

        time.sleep(0.6)  # still down for a couple of retry attempts
        RecordingHandler.received = []
        httpd = http.server.ThreadingHTTPServer(("127.0.0.1", port), RecordingHandler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + centrale_notify.RETRY_SECONDS
            while not RecordingHandler.received and time.monotonic() < deadline:
                time.sleep(0.05)
            time.sleep(4 * centrale_notify.RETRY_INTERVAL_SECONDS)
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=5)
        self.assertEqual(len(RecordingHandler.received), 1)
        self.assertEqual(RecordingHandler.received[0]["body"]["state"], "finished")


if __name__ == "__main__":
    unittest.main()
