"""The tier's own tmux-socket namespacing (task-161).

Every other module in this package trusts that its real tmux sessions
live somewhere no other run can see or kill. That used to be a fixed
`-L centrale-itest`, so it was only true when nobody else was running
the tier -- a maintainer, an agent, and the copy `scripts/release.sh`
runs against the staged snapshot all landed on ONE tmux server, where a
`kill-server` at any module's teardown tore down whatever the others had
just built. This module is the coverage for the property that replaced
it: one socket per test-run process, provably not shared, and nothing
left behind on that axis when the run ends.

The teardown half is what turns the convention into a fact. `kill-server`
does not unlink the socket file, so a per-run name litters one empty
socket per run unless teardown removes it and *checks* -- which is
exactly what AGENTS.md asks of a tier that launches real processes:
namespace every external-resource axis and assert the footprint is zero.

Run explicitly: python3 -m unittest tests_integration.test_tmux_socket_integration
"""

from __future__ import annotations

import os
import subprocess
import sys
import unittest

from tests_integration import base


def tearDownModule():
    base.assert_test_tmux_footprint_gone()


PROBE_CMD = 'sh -c "sleep 300"'


def socket_name_a_child_run_picks(env_overrides=None):
    """The socket name a *real* second run picks: a child process that
    imports `tests_integration.base` from scratch and prints its
    `TMUX_SOCKET`. Asking the module in-process would only prove that a
    function returns different strings; this proves that two processes
    running this tier do not meet."""
    env = dict(os.environ)
    env.pop(base.TMUX_SOCKET_ENV, None)
    env.update(env_overrides or {})
    proc = subprocess.run(
        [sys.executable, "-c",
         "from tests_integration import base; print(base.TMUX_SOCKET)"],
        cwd=base.CENTRALE_ROOT, env=env, capture_output=True, text=True, timeout=60,
    )
    if proc.returncode != 0:
        raise AssertionError(
            f"child import of tests_integration.base failed (exit "
            f"{proc.returncode}):\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}")
    return proc.stdout.strip()


@base.require_tools("tmux")
class TmuxSocketNamespaceTests(base.IntegrationCase):
    def sessions_on(self, socket_name=None):
        proc = base.tmux("list-sessions", "-F", "#{session_name}",
                         socket_name=socket_name, check=False)
        if proc.returncode != 0:
            return set()
        return {line.strip() for line in proc.stdout.splitlines() if line.strip()}

    def start_session(self, name, socket_name=None):
        base.tmux("new-session", "-d", "-s", name, PROBE_CMD,
                  socket_name=socket_name, check=True)
        if socket_name is None:
            self.track_session(name)
        return name

    # -- the name ---------------------------------------------------

    def test_each_test_run_process_gets_its_own_socket_name(self):
        """AC #1. Two fresh runs, and this one, all name different
        sockets -- with the shared prefix kept so a stray one is still
        recognisably ours."""
        first = socket_name_a_child_run_picks()
        second = socket_name_a_child_run_picks()

        names = [base.TMUX_SOCKET, first, second]
        self.assertEqual(len(set(names)), 3, f"two runs shared a socket: {names}")
        for name in names:
            self.assertTrue(name.startswith(base.TMUX_SOCKET_PREFIX + "-"), name)
            self.assertNotEqual(name, base.TMUX_SOCKET_PREFIX)

    def test_the_socket_name_is_overridable_from_the_environment(self):
        """AC #3. A caller outside the run -- a probe, or a debugging
        session that wants to attach to what a run is doing -- can pin
        the name."""
        chosen = f"{base.TMUX_SOCKET_PREFIX}-pinned-{os.getpid()}"
        self.assertEqual(
            socket_name_a_child_run_picks({base.TMUX_SOCKET_ENV: chosen}), chosen)

        # An empty or whitespace value is not a choice: it falls back to
        # a generated name rather than to `-L ` or `-L " "`.
        for blank in ("", "   "):
            with self.subTest(blank=repr(blank)):
                fallback = socket_name_a_child_run_picks({base.TMUX_SOCKET_ENV: blank})
                self.assertTrue(fallback.startswith(base.TMUX_SOCKET_PREFIX + "-"), fallback)

    # -- one socket, everywhere -------------------------------------

    def test_shim_direct_calls_and_pty_clients_all_land_on_one_socket(self):
        """AC #2. The three ways this tier reaches tmux -- a subprocess
        that finds the PATH shim (which is how a real server.py or
        spawn.py call gets redirected), the driver's own `-L` calls, and
        an attached pty client -- have to agree, or a test would inspect
        a socket its subject never wrote to."""
        with open(os.path.join(self.shim_dir, "tmux"), encoding="utf-8") as f:
            shim = f.read()
        self.assertIn(f"-L {base.TMUX_SOCKET} ", shim)

        name = f"{base.TMUX_SOCKET}-via-shim"
        self.track_session(name)
        # A bare `tmux`, resolved through PATH exactly as Centrale's own
        # code resolves it -- never with an explicit -L.
        base.run(["tmux", "new-session", "-d", "-s", name, PROBE_CMD],
                 env=self.env(), check=True)

        self.assertIn(name, self.sessions_on(),
                      "the shim wrote to a different socket than tmux() reads")
        self.assertTrue(os.path.exists(base.tmux_socket_path()),
                        f"no socket file at {base.tmux_socket_path()}")

        self.attach_client(name, columns=80, rows=24)
        self.assertIn("80x24", base.attached_client_sizes())

    # -- two runs side by side --------------------------------------

    def test_a_second_run_is_neither_observed_nor_killed_by_this_one(self):
        """AC #5, and the whole point of the task: the failure mode was
        one run's teardown killing another run's sessions mid-test."""
        peer = socket_name_a_child_run_picks()
        self.assertNotEqual(peer, base.TMUX_SOCKET)
        # Even if an assertion below fails, the peer socket must not
        # outlive this test.
        self.addCleanup(base.assert_test_tmux_footprint_gone, peer)

        mine = self.start_session(f"{base.TMUX_SOCKET}-mine")
        theirs = self.start_session(f"{peer}-theirs", socket_name=peer)

        # Neither run can see the other's sessions.
        self.assertEqual(self.sessions_on(), {mine})
        self.assertEqual(self.sessions_on(peer), {theirs})

        # This run finishing tears down this run only.
        base.assert_test_tmux_footprint_gone()
        self.assertEqual(self.sessions_on(), set())
        self.assertFalse(os.path.exists(base.tmux_socket_path()))
        self.assertEqual(
            self.sessions_on(peer), {theirs},
            "this run's teardown reached into the other run's tmux server")
        self.assertTrue(os.path.exists(base.tmux_socket_path(peer)))

        # AC #4: and when the other run finishes, it leaves nothing
        # either -- no server, and no socket file, which `kill-server`
        # alone would have left sitting in /tmp.
        base.assert_test_tmux_footprint_gone(peer)
        self.assertEqual(self.sessions_on(peer), set())
        self.assertFalse(os.path.exists(base.tmux_socket_path(peer)),
                         "kill-server does not unlink the socket; teardown must")

    def test_teardown_is_idempotent_and_survives_a_run_that_started_no_server(self):
        """Every module in this tier calls it, so it runs many times per
        run -- including for runs (test_release_integration.py) that
        never start a tmux server at all, where there is no socket file
        to remove."""
        untouched = base.new_tmux_socket_name()
        self.assertFalse(os.path.exists(base.tmux_socket_path(untouched)))
        base.assert_test_tmux_footprint_gone(untouched)
        base.assert_test_tmux_footprint_gone(untouched)

        used = base.new_tmux_socket_name()
        self.addCleanup(base.assert_test_tmux_footprint_gone, used)
        self.start_session(f"{used}-probe", socket_name=used)
        base.assert_test_tmux_footprint_gone(used)
        base.assert_test_tmux_footprint_gone(used)
        self.assertFalse(os.path.exists(base.tmux_socket_path(used)))


if __name__ == "__main__":
    unittest.main()
