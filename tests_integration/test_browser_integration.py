"""Integration coverage for browser.py's `backlog browser` launcher: a
real `backlog browser` process, launched against a real temp
backlog-init repo, actually serving; reuse while alive; what happens
after the tracked child dies; and a port pre-occupied by a foreign
process.

Run explicitly: python3 -m unittest tests_integration.test_browser_integration
"""

from __future__ import annotations

import socket
import sys
import unittest

from tests_integration import base

sys.path.insert(0, base.CENTRALE_ROOT)
import browser  # noqa: E402


def tearDownModule():
    base.assert_test_tmux_footprint_gone()


@base.require_tools("git", "backlog")
class BrowserLauncherIntegrationTests(base.IntegrationCase):
    def setUp(self):
        super().setUp()
        self.repo_path = base.init_backlog_repo(self.tmp_dir, name="browserproj")
        self.addCleanup(browser._reset_state)  # module-global state, shared across tests

    def _config(self, browser_port):
        return {
            "projects": [{"name": "browserproj", "path": self.repo_path, "browserPort": browser_port}],
            "browserPortBase": 6421,
        }

    def test_launch_serves_and_reuse_returns_same_url(self):
        port = self.track_port(base.free_port())
        config = self._config(port)

        result = browser.launch_or_reuse(config, "browserproj")
        self.track_proc(browser._state["browserproj"]["proc"])
        self.assertEqual(result, {"url": f"http://127.0.0.1:{port}"})

        base.wait_until_http_ok(result["url"], timeout=10.0)

        # Second call while the first process is still alive: reuse, not
        # a second launch -- same dict back, and still only one tracked
        # process for this project.
        result2 = browser.launch_or_reuse(config, "browserproj")
        self.assertEqual(result2, result)
        self.assertEqual(len(browser._state), 1)

    def test_killed_child_then_relaunch_still_works(self):
        port = self.track_port(base.free_port())
        config = self._config(port)

        browser.launch_or_reuse(config, "browserproj")
        self.track_proc(browser._state["browserproj"]["proc"])
        base.wait_until(lambda: base.port_listening(port), timeout=10.0)

        # Kill the *tracked* child the same way browser.py's own atexit
        # cleanup does (proc.terminate() on the single tracked pid, not
        # the whole process group) -- see docs/board.md: `backlog browser`
        # forks a real server process that gets reparented to the user's
        # systemd instance almost immediately, so this does NOT actually
        # free the port; the orphaned real server keeps listening on it.
        # kill_port(port) in cleanup (via track_port above and, for
        # whatever port the relaunch below picks, below) is what actually
        # reaps that orphan -- process-group signaling alone was found
        # unreliable once a child has been alive for a bit (see
        # find_pids_listening_on's docstring).
        tracked_proc = browser._state["browserproj"]["proc"]
        tracked_proc.terminate()
        base.wait_until(lambda: tracked_proc.poll() is not None, timeout=5.0,
                         message="tracked wrapper process never exited")

        # Relaunching must still succeed and hand back a URL that's
        # actually serving -- whether or not it's the same port depends
        # on whether the orphaned real server is still squatting the
        # original one (see above); this test intentionally does not
        # assert which, only that the result is always usable.
        second = browser.launch_or_reuse(config, "browserproj")
        self.track_proc(browser._state["browserproj"]["proc"])
        self.assertIn("url", second)
        self.track_port(int(second["url"].rsplit(":", 1)[1]))
        base.wait_until_http_ok(second["url"], timeout=10.0)

    def test_port_pre_occupied_by_foreign_process_walks_forward(self):
        """Documents CURRENT behavior (task-27's port-squatting fix, now
        landed and closed) when the assigned port is already held by
        something Centrale didn't launch: rather than failing or silently
        handing out a URL nobody's listening on, launch_or_reuse walks
        forward to the next free port and launches there (without
        --non-interactive, deliberately -- see browser.py's own
        docstring). Update this test if that contract changes."""
        occupied_port = base.free_port()
        foreign = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        foreign.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        foreign.bind(("127.0.0.1", occupied_port))
        foreign.listen(1)
        self.addCleanup(foreign.close)

        config = self._config(occupied_port)
        result = browser.launch_or_reuse(config, "browserproj")
        self.track_proc(browser._state["browserproj"]["proc"])

        actual_port = self.track_port(int(result["url"].rsplit(":", 1)[1]))
        self.assertNotEqual(actual_port, occupied_port,
                             "must not hand out a URL for a port a foreign process holds")
        base.wait_until_http_ok(result["url"], timeout=10.0)


if __name__ == "__main__":
    unittest.main()
