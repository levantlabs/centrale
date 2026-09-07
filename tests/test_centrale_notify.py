import http.server
import json
import os
import sys
import threading
import unittest
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
        post_event.assert_called_once_with("http://127.0.0.1:9/x", "working")

    def test_extra_argv_is_ignored(self):
        # codex always appends one extra JSON-payload argument describing
        # the completed turn -- must not change which state gets posted,
        # or trip up on it at all.
        with mock.patch.dict(os.environ, {"CENTRALE_EVENT_URL": "http://127.0.0.1:9/x"}):
            with mock.patch.object(centrale_notify, "_post_event") as post_event:
                centrale_notify.main(["centrale_notify.py", "finished", '{"turn": "payload"}'])
        post_event.assert_called_once_with("http://127.0.0.1:9/x", "finished")

    def test_post_event_failure_is_swallowed(self):
        # A hook must never be able to break or hang the agent it's
        # attached to -- any exception from the actual network call is
        # caught inside main(), never propagated.
        with mock.patch.dict(os.environ, {"CENTRALE_EVENT_URL": "http://127.0.0.1:9/x"}):
            with mock.patch.object(centrale_notify, "_post_event", side_effect=OSError("boom")):
                centrale_notify.main(["centrale_notify.py", "working"])  # must not raise


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
        centrale_notify._post_event(self._url(), "working")

        self.assertEqual(len(RecordingHandler.received), 1)
        received = RecordingHandler.received[0]
        self.assertEqual(received["body"], {"state": "working"})
        self.assertEqual(received["headers"]["Content-Type"], "application/json")

    def test_preserves_the_query_string_carrying_project_and_task(self):
        centrale_notify._post_event(self._url(), "finished")
        self.assertEqual(
            RecordingHandler.received[0]["path"],
            "/api/agent-event?project=my-app&task=TASK-2",
        )

    def test_main_end_to_end_against_the_real_local_server(self):
        with mock.patch.dict(os.environ, {"CENTRALE_EVENT_URL": self._url()}):
            centrale_notify.main(["centrale_notify.py", "waiting"])
        self.assertEqual(RecordingHandler.received[0]["body"], {"state": "waiting"})

    def test_connection_failure_raises_from_post_event(self):
        # A real connection failure (nothing listening on this port) --
        # _post_event itself is allowed to raise (main() is the one that
        # swallows it, see MainArgvHandlingTests); this confirms it
        # really is a plain, promptly-raised urllib exception, not
        # something that hangs until the 2s timeout.
        with self.assertRaises(Exception):
            centrale_notify._post_event("http://127.0.0.1:1/nope", "working")


if __name__ == "__main__":
    unittest.main()
