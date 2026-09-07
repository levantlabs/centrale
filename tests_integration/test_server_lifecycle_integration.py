"""Integration coverage for server.py's own process lifecycle: start a
real, unmodified server.py (deployed to an isolated temp directory, see
base.deploy_app) on an ephemeral port, have it launch a real `backlog
browser` child via POST /api/browser, SIGTERM the server, and check
whether that child actually gets cleaned up.

test_sigterm_cleans_up_launched_browser_child was marked
@unittest.expectedFailure from when it was first written (task-27) until
task-32: `backlog browser` is a node wrapper that immediately forks the
real listening server, which reparents to the user's systemd instance
almost immediately -- task-27's atexit cleanup and task-28's registry +
boot sweep only ever tracked that outer wrapper pid (what
launch_browser_process's Popen handle sees), never the real listener
underneath it, so killing the wrapper left the real server running
forever. task-32 fixed this: browser.py now also resolves the real
listener pid at launch time (server.resolve_listener_pid, via `ss`) and
records both in the registry; cleanup (atexit/SIGTERM) and the boot
sweep now kill both, each still independently guarded by the same
cmdline-match check as before. This test now exercises that fix for
real and passes. See test_boot_sweep_reaps_both_the_wrapper_and_the_deep_listener
below for the same fix demonstrated through a SIGKILL + fresh-boot-sweep
recovery instead of a graceful SIGTERM.

Run explicitly: python3 -m unittest tests_integration.test_server_lifecycle_integration
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import unittest
import urllib.request

from tests_integration import base


def tearDownModule():
    base.assert_test_tmux_footprint_gone()


@base.require_tools("git", "backlog")
class ServerLifecycleIntegrationTests(base.IntegrationCase):
    def setUp(self):
        super().setUp()
        self.repo_path = base.init_backlog_repo(self.tmp_dir, name="lifecycleproj")
        self.app_dir = base.deploy_app(os.path.join(self.tmp_dir, "app"))
        self.server_port = base.free_port()
        self.browser_port = base.free_port()

        config = {
            "port": self.server_port,
            "worktreeRoot": os.path.join(self.tmp_dir, "worktrees"),
            "browserPortBase": self.browser_port,
            "projects": [{"name": "lifecycleproj", "path": self.repo_path, "browserPort": self.browser_port}],
        }
        base.write_projects_json(self.app_dir, config)

        self.server_proc = None

    def _start_server(self):
        env = self.env()  # tmux-sandboxed PATH; this test doesn't spawn agents
        proc = subprocess.Popen(
            [sys.executable, "server.py"],
            cwd=self.app_dir,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,  # own process group, isolated from this test runner's
        )
        self.server_proc = proc
        self.track_proc(proc)  # guaranteed SIGKILL cleanup even if this test fails
        self.wait_for_http(f"http://127.0.0.1:{self.server_port}/api/board", timeout=15.0)
        return proc

    def _launch_real_browser_child(self):
        body = json.dumps({"project": "lifecycleproj"}).encode("utf-8")
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.server_port}/api/browser",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10.0) as resp:
            payload = json.loads(resp.read())
        self.assertEqual(payload["url"], f"http://127.0.0.1:{self.browser_port}")
        base.wait_until(lambda: base.port_listening(self.browser_port), timeout=10.0,
                         message="real backlog browser child never started listening")
        return payload

    def test_sigterm_cleans_up_launched_browser_child(self):
        self._start_server()
        self.track_port(self.browser_port)  # guaranteed reap regardless of outcome
        self._launch_real_browser_child()

        self.server_proc.send_signal(signal.SIGTERM)
        try:
            self.server_proc.wait(timeout=10.0)
        except subprocess.TimeoutExpired:
            self.fail("server.py did not exit within 10s of SIGTERM")

        base.wait_until(
            lambda: not base.port_listening(self.browser_port),
            timeout=5.0,
            message=(
                f"backlog browser child on port {self.browser_port} was still serving "
                f"after the server that launched it received SIGTERM and exited"
            ),
        )

    def test_boot_sweep_reaps_both_the_wrapper_and_the_deep_listener(self):
        """task-32: browser.py now resolves and records BOTH the wrapper
        pid (what launch_browser_process's Popen handle sees) and the
        real listener pid underneath it (server.resolve_listener_pid,
        via `ss` -- `backlog browser` forks a real, independently-
        listening server process that reparents to the user's systemd
        instance almost immediately, a different pid than the wrapper's).
        Using two real server processes that share one isolated registry
        (self.cache_home): the first is SIGKILLed (skips atexit
        entirely, leaving both pids alive and the registry entry
        un-cleared -- exactly the scenario sweep_orphaned_browsers exists
        for), and the second, started fresh, sweeps that same registry at
        its own boot and must reap both pids, not just the tracked
        wrapper -- the deep listener no longer survives, unlike before
        task-32's fix.
        """
        self._start_server()
        self.track_port(self.browser_port)
        self._launch_real_browser_child()

        registry_path = os.path.join(self.cache_home, "centrale", "browsers.json")
        with open(registry_path, encoding="utf-8") as f:
            entries = json.load(f)
        self.assertEqual(len(entries), 1, entries)
        wrapper_pid = entries[0]["pid"]
        listener_pid = entries[0].get("listenerPid")
        self.assertIsNotNone(listener_pid, "the listener pid should have been resolved at launch time")
        self.assertNotEqual(listener_pid, wrapper_pid, "the wrapper and the real listener are different processes")
        self.assertTrue(base.pid_alive(wrapper_pid))
        self.assertTrue(base.pid_alive(listener_pid))

        # Simulate a hard crash (SIGKILL, not SIGTERM): skips atexit
        # entirely, leaving the registry entry un-cleared -- exactly the
        # scenario sweep_orphaned_browsers exists for. A SIGKILL to the
        # *server* has no effect on the browser child it launched (no
        # process-group relationship ties them together), so both the
        # wrapper and the deep listener are still alive here.
        self.server_proc.kill()
        self.server_proc.wait(timeout=10.0)
        self.assertTrue(base.pid_alive(wrapper_pid))
        self.assertTrue(base.pid_alive(listener_pid))
        self.assertTrue(base.port_listening(self.browser_port))

        # A second server, started fresh, sweeps the same registry at
        # its own boot.
        second_app_dir = base.deploy_app(os.path.join(self.tmp_dir, "app2"))
        second_port = base.free_port()
        base.write_projects_json(second_app_dir, {
            "port": second_port,
            "worktreeRoot": os.path.join(self.tmp_dir, "worktrees2"),
            "browserPortBase": base.free_port(),
            "projects": [],
        })
        second_proc = subprocess.Popen(
            [sys.executable, "server.py"], cwd=second_app_dir, env=self.env(),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        self.track_proc(second_proc)
        self.wait_for_http(f"http://127.0.0.1:{second_port}/api/board", timeout=15.0)

        # Both the tracked wrapper AND the real listener underneath it
        # are now reaped -- task-32's fix, verified end to end.
        base.wait_until(lambda: not base.pid_alive(wrapper_pid), timeout=5.0,
                         message="boot sweep never reaped the registered wrapper pid")
        base.wait_until(lambda: not base.pid_alive(listener_pid), timeout=5.0,
                         message="boot sweep never reaped the deep listener pid")
        base.wait_until(lambda: not base.port_listening(self.browser_port), timeout=5.0,
                         message="backlog browser's port is still being served after the boot sweep")

    def test_server_itself_exits_cleanly_on_sigterm(self):
        """Narrower, currently-passing sibling of the test above: SIGTERM
        makes the server process itself exit promptly, independent of
        whether its launched children get cleaned up."""
        self._start_server()

        self.server_proc.send_signal(signal.SIGTERM)
        try:
            self.server_proc.wait(timeout=10.0)
        except subprocess.TimeoutExpired:
            self.fail("server.py did not exit within 10s of SIGTERM")
        self.assertIsNotNone(self.server_proc.returncode)


if __name__ == "__main__":
    unittest.main()
