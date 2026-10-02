"""The opt-in read-only status page (task-212, decision-6): its config,
its key, its listener's whole request surface, what its data may carry,
and how a failure to listen leaves the dashboard running.

Hermetic: every listener binds 127.0.0.1 on an ephemeral port, the key
file lives in a temporary directory, and the fleet snapshot is a fake.
Addresses in this file are documentation ranges (RFC 5737) or loopback."""
import contextlib
import http.client
import io
import json
import os
import re
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server  # noqa: E402
import settings  # noqa: E402
import status_page  # noqa: E402

REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KEY = "test-key-placeholder_-"
# A path the status page must never echo, from the temp directory rather
# than any account's home (scripts/scan_release.py refuses /home/<name>).
PRIVATE_DIR = os.path.join(tempfile.gettempdir(), "centrale-private-path")

# Strings planted in the fake snapshot wherever the dashboard's fleet data
# carries text the status page must never send.
SECRETS = ("PANE-LINE-SECRET", "MESSAGE-TEXT-SECRET", "SIGNAL-SECRET", "INBOX-ERROR-SECRET",
           "HISTORY-ERROR-SECRET", "HARVEST-REASON-SECRET", "OWNER-QUESTION-SECRET",
           "SESSION-NAME-SECRET", "DELIVERY-ERROR-SECRET")


def fake_fleet_snapshot():
    return {
        "timestamp": 200000.0, "window": 7200, "retention": 86400,
        "skippedLines": 0, "historyError": "HISTORY-ERROR-SECRET " + PRIVATE_DIR,
        "projects": [{"name": "app", "maxAgents": 3, "agentCount": 1},
                     {"name": "lib", "maxAgents": None, "agentCount": 0}],
        "agents": [{"project": "app", "taskId": "TASK-1", "session": "SESSION-NAME-SECRET",
                    "created": "100", "attached": False, "state": "waiting", "stateSince": 199900.0,
                    "agent": "claude", "agentKind": "claude",
                    "parked": {"reason": "SIGNAL-SECRET", "since": 1, "lastLine": "PANE-LINE-SECRET"}}],
        "history": [{"timestamp": 199000.0, "project": "app", "taskId": "TASK-1", "agent": "claude",
                     "state": "merge blocked", "harvest": {"reason": "HARVEST-REASON-SECRET"}}],
        "merges": [{"harvest": {"reason": "HARVEST-REASON-SECRET"}}],
        "messages": [{"text": "MESSAGE-TEXT-SECRET", "outcome": "failed"}],
        "deliverySkippedLines": 0, "deliveryError": "DELIVERY-ERROR-SECRET",
        "sessionPreviewMode": "interact",
        "needsYou": [
            {"kind": "permission", "project": "app", "taskId": "TASK-1", "agent": "claude", "since": 199950.0,
             "signal": "SIGNAL-SECRET", "lines": ["PANE-LINE-SECRET"], "capturedAt": 1.0},
            {"kind": "owner", "project": "app", "taskId": "TASK-2", "agent": "codex", "since": 199800.0,
             "signal": "SIGNAL-SECRET", "title": "t", "text": "OWNER-QUESTION-SECRET"},
            {"kind": "message", "project": "app", "taskId": "TASK-3", "agent": "claude", "since": 199700.0,
             "signal": "SIGNAL-SECRET", "text": "MESSAGE-TEXT-SECRET"}],
        "needsYouErrors": ["INBOX-ERROR-SECRET: " + os.path.join(PRIVATE_DIR, "repo")],
    }


class KeyFileMixin:
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.key_file = os.path.join(self.tmp.name, "state", "centrale", "status-page.key")
        env = mock.patch.dict(os.environ, {status_page.KEY_FILE_ENV: self.key_file})
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(status_page.stop)


class ConfigTests(KeyFileMixin, unittest.TestCase):
    def test_missing_is_off_with_the_default_port_and_no_address(self):
        self.assertEqual(status_page.normalize_config(None),
                         {"enabled": False, "port": 7421, "bind": None})

    def test_a_present_value_is_checked_like_every_other_key(self):
        for raw in ("on", [], {"enabled": "yes"}, {"enabled": True, "port": 0},
                    {"enabled": True, "port": "7421"}, {"enabled": True, "port": True},
                    {"enabled": True, "port": 70000}, {"enabled": True, "bind": 5},
                    {"enabled": True, "bind": "two words"}, {"enabled": True, "port": 7420}):
            with self.subTest(raw=raw), self.assertRaises(server.ConfigError):
                status_page.normalize_config(raw, main_port=7420)
        self.assertEqual(status_page.normalize_config({"enabled": True, "bind": "192.0.2.10"}),
                         {"enabled": True, "port": 7421, "bind": "192.0.2.10"})
        self.assertEqual(status_page.normalize_config({"enabled": True, "port": 7500, "bind": ""}),
                         {"enabled": True, "port": 7500, "bind": None})

    def test_load_config_carries_it_and_the_example_ships_it_off(self):
        path = os.path.join(self.tmp.name, "projects.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"projects": []}, f)
        self.assertEqual(server.load_config(path)["statusPage"],
                         {"enabled": False, "port": 7421, "bind": None})
        with open(os.path.join(REPO_DIR, "projects.example.json"), encoding="utf-8") as f:
            example = json.load(f)
        self.assertIs((example.get("statusPage") or {}).get("enabled", False), False)
        self.assertNotIn("bind", example.get("statusPage") or {})


class KeyTests(KeyFileMixin, unittest.TestCase):
    def test_generated_once_owner_only_and_then_reused(self):
        self.assertIsNone(status_page.read_key())
        key = status_page.ensure_key()
        self.assertGreaterEqual(len(key), 40)
        self.assertEqual(stat.S_IMODE(os.stat(self.key_file).st_mode), 0o600)
        self.assertEqual(status_page.ensure_key(), key)
        self.assertEqual(status_page.read_key(), key)

    def test_default_path_is_the_state_directory_beside_the_fleet_journal(self):
        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": "/state-home"}):
            os.environ.pop(status_page.KEY_FILE_ENV, None)
            self.assertEqual(status_page.key_path(), "/state-home/centrale/status-page.key")
            self.assertEqual(os.path.dirname(status_page.key_path()), str(server.fleet.history_path().parent))

    def test_comparison_is_constant_time(self):
        with mock.patch.object(status_page.hmac, "compare_digest", wraps=status_page.hmac.compare_digest) as cmp:
            self.assertTrue(status_page.key_matches(KEY, KEY))
            self.assertFalse(status_page.key_matches(KEY + "x", KEY))
        self.assertEqual(cmp.call_count, 2)


class ListenerTestBase(KeyFileMixin):
    def setUp(self):
        super().setUp()
        self.config = {"projects": [], "port": 7420, "refreshIntervalSeconds": 10,
                       "statusPage": {"enabled": True, "port": 7421, "bind": None}}
        self.httpd = status_page.StatusServer(self.config, KEY, 0, bind="127.0.0.1")
        self.port = self.httpd.server_address[1]
        thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)
        fleet_mock = mock.patch.object(server, "get_fleet", return_value=fake_fleet_snapshot())
        self.get_fleet = fleet_mock.start()
        self.addCleanup(fleet_mock.stop)
        # Nothing the listener does may reach a subprocess boundary.
        for name in ("run_git", "run_tmux", "run_backlog", "run_backlog_raw", "run_check_command",
                     "record_pane_capture", "send_session_input", "deliver_message"):
            patcher = mock.patch.object(server, name, side_effect=AssertionError(name + " reached"))
            patcher.start()
            self.addCleanup(patcher.stop)

    def request(self, method, path, headers=None, key=KEY):
        if key is not None:
            path += ("&" if "?" in path else "?") + "key=" + key
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            conn.request(method, path, headers=headers or {})
            response = conn.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            conn.close()


class RouteTests(ListenerTestBase, unittest.TestCase):
    def test_the_page_carries_the_key_into_every_asset_url(self):
        status, headers, body = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "text/html; charset=utf-8")
        html = body.decode()
        self.assertNotIn("{{KEY_QUERY}}", html)
        for asset in ("/static/styles.css", "/static/dom.js", "/static/fleet.js", "/static/phone.js"):
            self.assertIn(asset + "?key=" + KEY, html)

    def test_each_allowed_asset_is_served_with_its_type(self):
        for path, (name, ctype) in status_page.ASSETS.items():
            with self.subTest(path):
                status, headers, body = self.request("GET", path)
                self.assertEqual(status, 200)
                self.assertEqual(headers["Content-Type"], ctype)
                with open(os.path.join(server.STATIC_DIR, name), "rb") as f:
                    self.assertEqual(body, f.read())
                self.assertEqual(headers["Cache-Control"], "no-store")
                self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
                self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])

    def test_the_data_is_an_allowlist_with_no_text_beyond_names_and_states(self):
        status, _, body = self.request("GET", "/api/status")
        self.assertEqual(status, 200)
        for secret in SECRETS:
            self.assertNotIn(secret.encode(), body)
        self.assertNotIn(PRIVATE_DIR.encode(), body)
        data = json.loads(body)
        self.assertEqual(set(data), {"kind", "timestamp", "window", "refreshIntervalSeconds", "projects",
                                     "agents", "history", "needsYou", "historyUnavailable"})
        self.assertEqual(data["agents"], [{"project": "app", "taskId": "TASK-1", "agent": "claude",
                                           "state": "waiting", "stateSince": 199900.0, "parked": True}])
        self.assertEqual(data["history"], [{"project": "app", "taskId": "TASK-1", "agent": "claude",
                                            "state": "merge blocked", "timestamp": 199000.0}])
        self.assertEqual(data["projects"][0], {"name": "app", "maxAgents": 3, "agentCount": 1})
        needs = data["needsYou"]
        self.assertEqual(needs["count"], 3)
        self.assertEqual(needs["unavailable"], 1)
        self.assertEqual([i["kind"] for i in needs["items"]], ["message", "owner", "permission"])
        for item in needs["items"]:
            self.assertEqual(set(item), {"kind", "project", "taskId", "agent", "since"})
        self.assertTrue(data["historyUnavailable"])
        # The snapshot is taken without arming the dashboard's reply box.
        self.assertEqual(self.get_fleet.call_args.kwargs, {"arm_input": False})

    def test_an_unreadable_fleet_is_a_503_that_names_nothing(self):
        self.get_fleet.side_effect = server.BacklogError("tmux failed in " + os.path.join(PRIVATE_DIR, "secret-repo"))
        status, _, body = self.request("GET", "/api/status")
        self.assertEqual(status, 503)
        self.assertNotIn(PRIVATE_DIR.encode(), body)
        self.assertNotIn(b"tmux", body)

    def test_head_answers_without_a_body(self):
        for path in ("/", "/api/status", "/static/phone.js"):
            with self.subTest(path):
                status, headers, body = self.request("HEAD", path)
                self.assertEqual(status, 200)
                self.assertEqual(body, b"")
                self.assertGreater(int(headers["Content-Length"]), 0)

    def dashboard_paths(self):
        with open(os.path.join(REPO_DIR, "server.py"), encoding="utf-8") as f:
            routes = set(re.findall(r'path == "(/[^"]*)"', f.read()))
        self.assertGreater(len(routes), 20, sorted(routes))  # not vacuous
        return sorted(routes - {"/", "/favicon.ico"})

    def test_every_dashboard_route_is_404_here(self):
        for path in self.dashboard_paths():
            with self.subTest(path):
                status, _, body = self.request("GET", path + "?project=app&task=TASK-1")
                self.assertEqual(status, 404)
                self.assertEqual(json.loads(body), {"error": "not found"})

    def test_path_tricks_name_nothing_else(self):
        for path in ("/static/index.html", "/static/state.js", "/static/settings.js", "/static/pane.js",
                     "/static/agent-guide.md", "/static/phone.html", "/index.html", "/server.py",
                     "/projects.json", "/static/../server.py", "/static/%2e%2e/server.py",
                     "/static/..%2fserver.py", "/static//phone.js", "/static/./phone.js",
                     "/static/phone.js/", "/static/PHONE.JS", "/api/status/",
                     "/API/STATUS", "/api/status/../fleet", "/api/fleet", "/api//fleet",
                     "/%2fapi/fleet", "/static", "/static/", "/api", "http://192.0.2.10/api/fleet"):
            with self.subTest(path):
                status, _, body = self.request("GET", path)
                self.assertEqual(status, 404, body)

    def test_a_doubled_leading_slash_is_the_same_path(self):
        # http.server itself collapses a leading "//" before any handler
        # sees the path, so it can only ever name the same resource.
        self.assertEqual(self.request("GET", "//api/status")[2], self.request("GET", "/api/status")[2])

    def test_every_other_method_is_405_and_does_nothing(self):
        for method in ("POST", "PUT", "DELETE", "PATCH", "OPTIONS", "TRACE", "PROPFIND", "FOO"):
            for path in ("/", "/api/status", "/api/settings", "/api/spawn", "/api/harvest", "/nowhere"):
                with self.subTest(method=method, path=path):
                    status, headers, _ = self.request(method, path, headers={"Content-Type": "application/json"})
                    self.assertEqual(status, 405)
                    self.assertEqual(headers["Allow"], "GET, HEAD")
        self.get_fleet.assert_not_called()

    def test_without_the_right_key_everything_is_the_unknown_path_404(self):
        _, _, unknown = self.request("GET", "/nowhere")
        attempts = [(None, ""), ("", ""), (KEY.upper(), ""), (KEY[:-1], ""), (KEY + "x", ""),
                    (KEY + "&key=" + KEY, ""), ("%20" + KEY, "")]
        for key, _ in attempts:
            for method in ("GET", "HEAD", "POST", "DELETE"):
                for path in ("/", "/api/status", "/static/phone.js", "/favicon.ico", "/api/fleet"):
                    with self.subTest(key=key, method=method, path=path):
                        status, _, body = self.request(method, path, key=key)
                        self.assertEqual(status, 404)
                        if method != "HEAD":
                            self.assertEqual(body, unknown)
        self.get_fleet.assert_not_called()

    def test_a_cross_site_browser_request_is_refused(self):
        status, _, _ = self.request("GET", "/api/status", headers={"Sec-Fetch-Site": "cross-site"})
        self.assertEqual(status, 404)
        for value in ("same-origin", "none"):
            status, _, _ = self.request("GET", "/api/status", headers={"Sec-Fetch-Site": value})
            self.assertEqual(status, 200)

    def test_no_response_contains_dashboard_content(self):
        bodies = b""
        for path in ["/", "/api/status", *status_page.ASSETS, *self.dashboard_paths()]:
            bodies += self.request("GET", path)[2]
        for secret in SECRETS:
            self.assertNotIn(secret.encode(), bodies)
        # Nothing the dashboard's own document or its controls are made of.
        for marker in (b'id="settings-open"', b'"harvestMode"', b'"agentEntries"', b"session-input"):
            self.assertNotIn(marker, bodies)


class NoSideEffectTests(KeyFileMixin, unittest.TestCase):
    def test_the_real_snapshot_never_arms_the_reply_box(self):
        config = {"projects": [{"name": "app", "path": "/repo"}], "worktreeRoot": self.tmp.name,
                  "sessionPreview": {"mode": "interact"}}
        import fleet
        journal = fleet.Journal(os.path.join(self.tmp.name, "h.jsonl"), clock=lambda: 200000)
        lifecycle = dict(state="waiting", stateSince=199900, agent="helper", agentKind="codex")
        with mock.patch.object(server, "OWNER_QUESTIONS_TTL", 0.0), \
             mock.patch.object(server, "get_board", return_value={"projects": [{"name": "app", "tasks": []}]}), \
             mock.patch.object(server, "fleet_history", journal), \
             mock.patch.object(server, "list_sessions", return_value=[{"name": "centrale-app-task-1", "created": "1"}]), \
             mock.patch.object(server, "get_fleet_lifecycle", return_value=lifecycle), \
             mock.patch.object(server, "capture_session_pane", return_value=["Allow command?", "› 1. Yes", "  2. No"]), \
             mock.patch.object(server, "parked_status", return_value=None), \
             mock.patch.object(server, "read_delivery_log", return_value=([], 0)), \
             mock.patch.object(server, "record_pane_capture") as armed:
            data = status_page.status_snapshot(config)
            self.assertEqual([i["kind"] for i in data["needsYou"]["items"]], ["permission"])
            armed.assert_not_called()
            server.get_fleet(config)  # the dashboard's own read still arms it
            armed.assert_called_once_with("centrale-app-task-1")


class StartupTests(KeyFileMixin, unittest.TestCase):
    def config(self, **page):
        return {"projects": [], "port": 7420, "statusPage": {"enabled": False, "port": 7421, "bind": None, **page}}

    def test_off_opens_nothing_and_creates_no_key(self):
        with mock.patch.object(status_page, "StatusServer") as listener:
            self.assertIsNone(status_page.apply(self.config(), log=io.StringIO()))
            self.assertIsNone(status_page.apply({"projects": []}, log=io.StringIO()))
        listener.assert_not_called()
        self.assertFalse(os.path.exists(self.key_file))
        self.assertEqual(status_page.status(self.config())["links"], [])

    def test_status_and_log_label_only_the_local_link(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        log = io.StringIO()
        config = self.config(enabled=True, port=port)
        with mock.patch.object(status_page, "interface_addresses", return_value=[("eth0", "192.0.2.10")]), \
             mock.patch.object(status_page.socket, "gethostname", return_value="workstation"):
            self.assertIsNone(status_page.apply(config, log=log))
            state = status_page.status(config)
        key = status_page.read_key()
        numeric, local = f"http://192.0.2.10:{port}/?key={key}", f"http://workstation.local:{port}/?key={key}"
        self.assertEqual(state["links"], [numeric, local])
        self.assertEqual(state["labelledLinks"], [{"url": numeric, "label": None},
                                                  {"url": local, "label": status_page.LOCAL_LINK_NOTE}])
        self.assertIn(f"  {numeric}\n", log.getvalue())
        self.assertIn(f"  {local}  ({status_page.LOCAL_LINK_NOTE})\n", log.getvalue())

    def test_on_listens_creates_the_key_and_prints_the_links(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        log = io.StringIO()
        config = self.config(enabled=True, port=port, bind="127.0.0.1")
        with mock.patch.object(status_page, "interface_addresses", return_value=[("eth0", "192.0.2.10")]), \
             mock.patch.object(status_page.socket, "gethostname", return_value="workstation"):
            self.assertIsNone(status_page.apply(config, log=log))
            state = status_page.status(config)
        key = status_page.read_key()
        self.assertTrue(state["running"])
        # Bound to loopback: the one address that answers, and no .local form.
        self.assertEqual(state["links"], [f"http://127.0.0.1:{port}/?key={key}"])
        self.assertIn(f"http://127.0.0.1:{port}/?key={key}", log.getvalue())
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/static/phone.js?key=" + key)
        self.assertEqual(conn.getresponse().status, 200)
        conn.close()
        # Off again (as a Settings save does): the port closes at once.
        config["statusPage"]["enabled"] = False
        status_page.apply(config, log=log)
        self.assertFalse(status_page.status(config)["running"])
        with socket.socket() as s:
            self.assertNotEqual(s.connect_ex(("127.0.0.1", port)), 0)

    def test_a_port_in_use_is_reported_and_never_raised(self):
        with socket.socket() as busy:
            busy.bind(("127.0.0.1", 0))
            busy.listen()
            port = busy.getsockname()[1]
            log = io.StringIO()
            config = self.config(enabled=True, port=port, bind="127.0.0.1")
            error = status_page.apply(config, log=log)
        self.assertTrue(error)
        self.assertIn(f"could not listen on port {port} on 127.0.0.1", log.getvalue())
        self.assertIn("The dashboard is running as usual", log.getvalue())
        state = status_page.status(config)
        self.assertFalse(state["running"])
        self.assertEqual(state["error"], error)

    def run_main(self, page):
        """main() with every boundary faked; returns (stderr, dashboard server mock)."""
        config = self.config(**page)
        config.update(subprocessTimeoutSeconds=30, zeroConfig=False)
        dashboard = mock.MagicMock()
        dashboard.serve_forever.side_effect = KeyboardInterrupt
        stderr, stdout = io.StringIO(), io.StringIO()
        import browser, harvest, spawn
        with mock.patch.object(server.sys, "argv", ["server.py"]), \
             mock.patch.object(server, "install_terminate_handlers"), \
             mock.patch.object(server, "load_config", return_value=config), \
             mock.patch.object(server.fleet, "Journal"), \
             mock.patch.object(server, "detect_capabilities", return_value={"tmux": False}), \
             mock.patch.object(server, "detect_version", return_value="test"), \
             mock.patch.object(browser, "sweep_orphaned_browsers", return_value={"killed": [], "stale": []}), \
             mock.patch.object(spawn, "release_stale_task_locks", return_value=[]), \
             mock.patch.object(harvest, "start_auto_harvest_thread"), \
             mock.patch.object(server, "CentraleHTTPServer", return_value=dashboard) as dashboard_cls, \
             contextlib.redirect_stderr(stderr), contextlib.redirect_stdout(stdout):
            server.main()
        dashboard_cls.assert_called_once()
        self.assertEqual(dashboard_cls.call_args.args[0], ("127.0.0.1", 7420))
        return stderr.getvalue(), dashboard

    def test_main_with_no_setting_starts_no_second_listener(self):
        with mock.patch.object(status_page, "StatusServer") as listener:
            _, dashboard = self.run_main({})
        listener.assert_not_called()
        dashboard.serve_forever.assert_called_once()

    def test_main_still_serves_the_dashboard_when_the_page_cannot_listen(self):
        with mock.patch.object(status_page, "StatusServer", side_effect=OSError(98, "Address already in use")):
            stderr, dashboard = self.run_main({"enabled": True})
        self.assertIn("the read-only status page did not start", stderr)
        self.assertIn("Address already in use", stderr)
        dashboard.serve_forever.assert_called_once()


class LinkTests(KeyFileMixin, unittest.TestCase):
    IP_JSON = json.dumps([
        {"ifname": "lo", "addr_info": [{"family": "inet", "local": "127.0.0.1", "scope": "host"},
                                       {"family": "inet6", "local": "::1", "scope": "host"}]},
        {"ifname": "eth0", "addr_info": [{"family": "inet6", "local": "2001:db8::10", "scope": "global"},
                                         {"family": "inet6", "local": "2001:db8::beef", "scope": "global", "temporary": True},
                                         {"family": "inet6", "local": "fe80::1", "scope": "link"},
                                         {"family": "inet", "local": "192.0.2.10", "scope": "global"}]},
        {"ifname": "tun0", "addr_info": [{"family": "inet", "local": "198.51.100.7", "scope": "global"}]},
        {"ifname": "docker0", "addr_info": [{"family": "inet", "local": "203.0.113.1", "scope": "global"}]},
        {"ifname": "br-1a2b3c", "addr_info": [{"family": "inet", "local": "203.0.113.17", "scope": "global"}]},
        {"ifname": "veth9f8e", "addr_info": [{"family": "inet6", "local": "2001:db8:1::5", "scope": "global"}]},
        {"ifname": "virbr0", "addr_info": [{"family": "inet", "local": "203.0.113.33", "scope": "global"}]},
        {"ifname": "cni0", "addr_info": [{"family": "inet", "local": "203.0.113.49", "scope": "global"}]},
        {"ifname": "flannel.1", "addr_info": [{"family": "inet", "local": "203.0.113.65", "scope": "global"}]},
        {"ifname": "podman1", "addr_info": [{"family": "inet", "local": "203.0.113.81", "scope": "global"}]},
        {"ifname": "lxcbr0", "addr_info": [{"family": "inet", "local": "203.0.113.97", "scope": "global"}]},
        {"ifname": "lxdbr0", "addr_info": [{"family": "inet", "local": "203.0.113.113", "scope": "global"}]},
    ])

    def links(self, bind=None):
        config = {"statusPage": {"enabled": True, "port": 7421, "bind": bind}}
        with mock.patch.object(status_page, "run_ip_addr",
                               return_value=subprocess.CompletedProcess([], 0, self.IP_JSON, "")), \
             mock.patch.object(status_page.socket, "gethostname", return_value="workstation.example"):
            return status_page.status_links(config, "k")

    def test_one_link_per_reachable_address_then_hostname_local(self):
        self.assertEqual(self.links(), ["http://192.0.2.10:7421/?key=k", "http://198.51.100.7:7421/?key=k",
                                        "http://[2001:db8::10]:7421/?key=k", "http://workstation.local:7421/?key=k"])

    def test_container_and_virtual_bridges_get_no_link(self):
        links = self.links()
        for address in ("203.0.113.", "2001:db8:1::5"):
            self.assertFalse([l for l in links if address in l], links)
        for name in ("docker0", "br-1a2b3c", "veth9f8e", "virbr0", "cni0", "flannel.1", "podman1", "lxcbr0", "lxdbr0"):
            self.assertTrue(status_page.is_bridge_interface(name), name)
        for name in ("eth0", "wlan0", "tailscale0", "wg0", "tun0", "enp3s0", "bridge0", None):
            self.assertFalse(status_page.is_bridge_interface(name), name)

    def test_a_bridge_named_as_the_bind_still_gets_its_link(self):
        self.assertEqual(self.links("docker0"), ["http://203.0.113.1:7421/?key=k", "http://workstation.local:7421/?key=k"])

    def test_bridges_are_still_bound(self):
        # Only the links are filtered: the listener's address is unchanged.
        with mock.patch.object(status_page.socket, "if_nameindex", return_value=[(1, "lo"), (2, "docker0")]):
            server_ = status_page.StatusServer.__new__(status_page.StatusServer)
            with mock.patch.object(status_page.http.server.ThreadingHTTPServer, "__init__", return_value=None) as base:
                status_page.StatusServer.__init__(server_, {}, "k", 7421)
        self.assertEqual(base.call_args.args[0], ("::", 7421))

    def test_a_bind_restriction_narrows_the_links(self):
        self.assertEqual(self.links("tun0"), ["http://198.51.100.7:7421/?key=k", "http://workstation.local:7421/?key=k"])
        self.assertEqual(self.links("127.0.0.1"), ["http://127.0.0.1:7421/?key=k"])
        self.assertEqual(self.links("192.0.2.10"), ["http://192.0.2.10:7421/?key=k", "http://workstation.local:7421/?key=k"])

    def test_without_ip_the_outbound_address_is_used(self):
        with mock.patch.object(status_page, "run_ip_addr", return_value=None), \
             mock.patch.object(status_page, "primary_address", return_value="192.0.2.10"):
            self.assertEqual(status_page.interface_addresses(), [(None, "192.0.2.10")])


class DoctorTests(KeyFileMixin, unittest.TestCase):
    def doctor(self, page, **patches):
        path = os.path.join(self.tmp.name, "projects.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"projects": [], "statusPage": page}, f)
        with mock.patch.object(server, "which", return_value=None), \
             mock.patch.object(server, "probe_running_server", return_value=None), \
             mock.patch.object(status_page, "interface_addresses", return_value=[("eth0", "192.0.2.10")]), \
             mock.patch.object(status_page.socket, "gethostname", return_value="workstation"):
            lines, _ = server.run_doctor_check(config_path=path)
        return [line for line in lines if "status page" in line]

    def test_off_says_nothing(self):
        self.assertEqual(self.doctor({"enabled": False}), [])
        self.assertFalse(os.path.exists(self.key_file))

    def test_on_and_free_prints_the_links(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        lines = self.doctor({"enabled": True, "port": port, "bind": "127.0.0.1"})
        key = status_page.read_key()
        self.assertTrue(lines[0].startswith("[PASS] read-only status page: port"), lines)
        self.assertIn(f"[PASS] read-only status page link: http://127.0.0.1:{port}/?key={key}", lines)
        lines = self.doctor({"enabled": True, "port": port})
        self.assertIn(f"[PASS] read-only status page link: http://192.0.2.10:{port}/?key={key}", lines)
        self.assertIn(f"[PASS] read-only status page link: http://workstation.local:{port}/?key={key}  ({status_page.LOCAL_LINK_NOTE})", lines)
        self.assertEqual(status_page.LOCAL_LINK_NOTE, "home Wi-Fi only; works on Apple devices; not over a VPN")

    def test_a_port_held_by_something_else_is_a_warning(self):
        with socket.socket() as busy:
            busy.bind(("127.0.0.1", 0))
            busy.listen()
            port = busy.getsockname()[1]
            with mock.patch.object(status_page, "probe_status_page", return_value=False):
                lines = self.doctor({"enabled": True, "port": port, "bind": "127.0.0.1"})
        self.assertTrue(lines[0].startswith(f"[WARN] read-only status page: cannot listen on port {port}"), lines)
        self.assertIn("Centrale will still start", lines[0])

    def test_a_port_held_by_the_running_centrale_passes(self):
        with socket.socket() as busy:
            busy.bind(("127.0.0.1", 0))
            busy.listen()
            port = busy.getsockname()[1]
            with mock.patch.object(status_page, "probe_status_page", return_value=True):
                lines = self.doctor({"enabled": True, "port": port, "bind": "127.0.0.1"})
        self.assertIn("a running Centrale is serving it", lines[0])


class LinkLabelTests(unittest.TestCase):
    def test_only_the_local_form_is_labelled(self):
        self.assertIsNone(status_page.link_label("http://192.0.2.10:7421/?key=k"))
        self.assertIsNone(status_page.link_label("http://[2001:db8::10]:7421/?key=k"))
        self.assertEqual(status_page.link_label("http://myhost.local:7421/?key=k"), status_page.LOCAL_LINK_NOTE)
        self.assertEqual(status_page.labelled_link("http://192.0.2.10:7421/?key=k"), "http://192.0.2.10:7421/?key=k")
        self.assertEqual(status_page.labelled_link("http://myhost.local:7421/?key=k"),
                         f"http://myhost.local:7421/?key=k  ({status_page.LOCAL_LINK_NOTE})")


class SettingsTests(KeyFileMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.path = os.path.join(self.tmp.name, "projects.json")
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump({"port": 7420, "projects": []}, f)
        self.config = server.load_config(self.path)

    def test_get_reports_it_off_with_no_links(self):
        page = settings.current_settings(self.config)["statusPage"]
        self.assertEqual(page, {"enabled": False, "port": 7421, "bind": None, "running": False,
                                "error": None, "links": [], "labelledLinks": []})

    def test_a_save_writes_switch_port_and_bind_but_never_the_key(self):
        settings.apply_settings(self.config, {"statusPage": {"enabled": True, "port": 7500, "bind": "tun0"}},
                                path=self.path)
        with open(self.path, encoding="utf-8") as f:
            raw = json.load(f)
        self.assertEqual(raw["statusPage"], {"enabled": True, "port": 7500, "bind": "tun0"})
        self.assertEqual(self.config["statusPage"], {"enabled": True, "port": 7500, "bind": "tun0"})
        settings.apply_settings(self.config, {"statusPage": {"bind": ""}}, path=self.path)
        with open(self.path, encoding="utf-8") as f:
            text = f.read()
        self.assertEqual(json.loads(text)["statusPage"], {"enabled": True, "port": 7500})
        self.assertNotIn("key", json.loads(text)["statusPage"])

    def test_a_save_without_it_leaves_projects_json_without_it(self):
        settings.apply_settings(self.config, {"refreshIntervalSeconds": 15}, path=self.path)
        with open(self.path, encoding="utf-8") as f:
            self.assertNotIn("statusPage", json.load(f))

    def test_bad_values_are_field_errors(self):
        with self.assertRaises(settings.ValidationError) as ctx:
            settings.apply_settings(self.config, {"statusPage": {"enabled": "yes", "port": 7420, "bind": "a b",
                                                                 "key": "chosen"}}, path=self.path)
        self.assertEqual(set(ctx.exception.fields), {"statusPage.enabled", "statusPage.port",
                                                     "statusPage.bind", "statusPage.key"})
        with self.assertRaises(settings.SettingsError):
            settings.apply_settings(self.config, {"statusPage": True}, path=self.path)


if __name__ == "__main__":
    unittest.main()
