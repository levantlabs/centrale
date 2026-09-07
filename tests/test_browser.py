import contextlib
import io
import itertools
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import browser  # noqa: E402
import server  # noqa: E402


def make_config(projects, browser_port_base=6421):
    return {
        "port": 0,
        "worktreeRoot": "/tmp/does-not-matter",
        "projects": projects,
        "browserPortBase": browser_port_base,
    }


class FakeProc:
    """Stand-in for a subprocess.Popen handle. `alive=False` simulates a
    process that has already exited (poll() returns its exit code)."""

    def __init__(self, pid=42, alive=True):
        self.pid = pid
        self._alive = alive
        self.terminated = False

    def poll(self):
        return None if self._alive else 0

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.terminated = True


def _free_then_taken():
    """port_is_free side_effect for the ordinary happy path: free before
    launch (so the assigned port is used as-is), taken immediately after
    (so _verify_launch succeeds on its first poll, no sleep_fn calls).
    Cycles indefinitely, so it's safe to reuse across however many launch
    attempts a single test makes -- a reuse (no new launch) just consumes
    none of it."""
    return itertools.cycle([True, False])


def _never_sleep(seconds):
    """sleep_fn for tests: makes _verify_launch's poll loop instant, and
    lets a timeout-path test assert on call count instead of waiting out
    the real LAUNCH_VERIFY_TIMEOUT."""


def _use_temp_registry(test_case):
    """Redirects browser.registry_path() to a throwaway temp file for the
    duration of test_case, so a real launch_or_reuse() or
    sweep_orphaned_browsers() call in a test never reads or writes the
    user's actual ~/.cache/centrale/browsers.json. Returns the temp path,
    in case a test wants to inspect it directly."""
    tmp_dir = tempfile.mkdtemp(prefix="centrale-browser-registry-")
    test_case.addCleanup(shutil.rmtree, tmp_dir, ignore_errors=True)
    tmp_path = os.path.join(tmp_dir, "browsers.json")
    patcher = mock.patch.object(browser, "registry_path", return_value=tmp_path)
    patcher.start()
    test_case.addCleanup(patcher.stop)
    return tmp_path


def _capture_stderr(test_case):
    """Captures sys.stderr for the rest of test_case, returning the
    buffer. An unresolved listener pid makes launch_or_reuse print a
    real user-facing warning to stderr (task-106); these tests trigger
    that path deliberately and by the dozen, so without this the warning
    lands in the runner's output and a clean green run reads like a
    broken build. Silenced at the test boundary on purpose -- the
    message itself is worth keeping for the user it was written for."""
    buffer = io.StringIO()
    redirect = contextlib.redirect_stderr(buffer)
    redirect.__enter__()
    test_case.addCleanup(redirect.__exit__, None, None, None)
    return buffer


def _stub_resolve_listener_pid(test_case, return_value=None):
    """launch_or_reuse (task-32) resolves the real listener pid via
    server.resolve_listener_pid, which shells out to a real `ss` --
    every test that exercises a real launch_or_reuse() call needs this
    mocked so it never does, same reasoning as _use_temp_registry.
    Defaults to None (unresolved), matching this module's own graceful
    "couldn't resolve it, cleanup only tracks the wrapper" fallback --
    the specific listener-pid-tracking behavior gets its own dedicated
    test class instead of overloading every other test with it. That
    fallback prints a warning, so this captures stderr too."""
    patcher = mock.patch.object(server, "resolve_listener_pid", return_value=return_value)
    patcher.start()
    test_case.addCleanup(patcher.stop)
    if return_value is None:
        _capture_stderr(test_case)


class BrowserLaunchTests(unittest.TestCase):
    def setUp(self):
        browser._reset_state()
        self.addCleanup(browser._reset_state)
        self.registry_path = _use_temp_registry(self)
        _stub_resolve_listener_pid(self)

    def test_launch_uses_backlog_browser_argv_and_default_port(self):
        config = make_config([{"name": "my-app", "path": "/repos/my-app", "browserPort": None}])
        fake_proc = FakeProc(alive=True)
        with mock.patch.object(server, "launch_browser_process", return_value=fake_proc) as launch, \
             mock.patch.object(server, "port_is_free", side_effect=_free_then_taken()):
            result = browser.launch_or_reuse(config, "my-app", sleep_fn=_never_sleep)

        self.assertEqual(result, {"url": "http://127.0.0.1:6421"})
        launch.assert_called_once_with(
            ["backlog", "browser", "--port", "6421", "--no-open", "--non-interactive"],
            cwd="/repos/my-app",
        )

    def test_port_assignment_uses_project_index(self):
        config = make_config([
            {"name": "my-app", "path": "/repos/my-app", "browserPort": None},
            {"name": "my-tool", "path": "/repos/my-tool", "browserPort": None},
        ])
        with mock.patch.object(server, "launch_browser_process", return_value=FakeProc()) as launch, \
             mock.patch.object(server, "port_is_free", side_effect=_free_then_taken()):
            browser.launch_or_reuse(config, "my-tool", sleep_fn=_never_sleep)

        (argv,), kwargs = launch.call_args
        self.assertIn("6422", argv)

    def test_per_project_browserPort_override_wins(self):
        config = make_config([
            {"name": "my-app", "path": "/repos/my-app", "browserPort": 9999},
            {"name": "my-tool", "path": "/repos/my-tool", "browserPort": None},
        ])
        with mock.patch.object(server, "launch_browser_process", return_value=FakeProc()) as launch, \
             mock.patch.object(server, "port_is_free", side_effect=_free_then_taken()):
            result = browser.launch_or_reuse(config, "my-app", sleep_fn=_never_sleep)

        self.assertEqual(result, {"url": "http://127.0.0.1:9999"})
        (argv,), _ = launch.call_args
        self.assertIn("9999", argv)

    def test_unknown_project_raises_404_without_launching(self):
        config = make_config([{"name": "my-app", "path": "/repos/my-app", "browserPort": None}])
        with mock.patch.object(server, "launch_browser_process") as launch:
            with self.assertRaises(browser.BrowserError) as ctx:
                browser.launch_or_reuse(config, "nonexistent")
        self.assertEqual(ctx.exception.status, 404)
        launch.assert_not_called()

    def test_missing_project_field_raises_400(self):
        config = make_config([{"name": "my-app", "path": "/repos/my-app", "browserPort": None}])
        with self.assertRaises(browser.BrowserError) as ctx:
            browser.launch_or_reuse(config, "")
        self.assertEqual(ctx.exception.status, 400)

    def test_reuse_when_process_still_alive(self):
        config = make_config([{"name": "my-app", "path": "/repos/my-app", "browserPort": None}])
        with mock.patch.object(server, "launch_browser_process", return_value=FakeProc(alive=True)) as launch, \
             mock.patch.object(server, "port_is_free", side_effect=_free_then_taken()):
            first = browser.launch_or_reuse(config, "my-app", sleep_fn=_never_sleep)
            second = browser.launch_or_reuse(config, "my-app", sleep_fn=_never_sleep)

        self.assertEqual(first, second)
        launch.assert_called_once()

    def test_relaunch_when_process_has_died(self):
        # The first process is alive (and passes launch verification) at
        # spawn time, but has since died by the time the second call's
        # reuse-check looks at it (e.g. the user closed it) -- distinct
        # from BrowserLaunchVerificationTests, which covers a process
        # that's already dead the instant it's launched.
        config = make_config([{"name": "my-app", "path": "/repos/my-app", "browserPort": None}])
        first_proc = FakeProc(pid=1, alive=True)
        second_proc = FakeProc(pid=2, alive=True)
        with mock.patch.object(server, "launch_browser_process", side_effect=[first_proc, second_proc]) as launch, \
             mock.patch.object(server, "port_is_free", side_effect=_free_then_taken()):
            first = browser.launch_or_reuse(config, "my-app", sleep_fn=_never_sleep)
            first_proc._alive = False  # dies sometime after the first launch
            second = browser.launch_or_reuse(config, "my-app", sleep_fn=_never_sleep)

        self.assertEqual(first, second)  # same port both times
        self.assertEqual(launch.call_count, 2)

    def test_launch_failure_becomes_browser_error_500(self):
        config = make_config([{"name": "my-app", "path": "/repos/my-app", "browserPort": None}])
        with mock.patch.object(server, "launch_browser_process", side_effect=FileNotFoundError("no backlog")), \
             mock.patch.object(server, "port_is_free", side_effect=_free_then_taken()):
            with self.assertRaises(browser.BrowserError) as ctx:
                browser.launch_or_reuse(config, "my-app", sleep_fn=_never_sleep)
        self.assertEqual(ctx.exception.status, 500)

    def test_atexit_cleanup_terminates_tracked_processes(self):
        config = make_config([{"name": "my-app", "path": "/repos/my-app", "browserPort": None}])
        fake_proc = FakeProc(alive=True)
        with mock.patch.object(server, "launch_browser_process", return_value=fake_proc), \
             mock.patch.object(server, "port_is_free", side_effect=_free_then_taken()):
            browser.launch_or_reuse(config, "my-app", sleep_fn=_never_sleep)

        browser._cleanup_all()
        self.assertTrue(fake_proc.terminated)


class BrowserListenerPidTests(unittest.TestCase):
    """Task-32: `backlog browser` is a node wrapper that immediately
    forks the REAL listening server, which reparents away within about a
    second -- killing only the wrapper pid (what launch_browser_process's
    Popen handle tracks) leaves that real server running forever. These
    cover resolving and recording the real listener pid at launch time,
    and killing BOTH pids -- each still independently guarded by the
    cmdline-match check -- from in-process cleanup and the boot sweep."""

    def setUp(self):
        browser._reset_state()
        self.addCleanup(browser._reset_state)
        self.registry_path = _use_temp_registry(self)

    def _launch(self, listener_pid, wrapper_pid=42):
        config = make_config([{"name": "my-app", "path": "/repos/my-app", "browserPort": None}])
        fake_proc = FakeProc(pid=wrapper_pid, alive=True)
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr), \
             mock.patch.object(server, "launch_browser_process", return_value=fake_proc), \
             mock.patch.object(server, "port_is_free", side_effect=_free_then_taken()), \
             mock.patch.object(server, "resolve_listener_pid", return_value=listener_pid):
            browser.launch_or_reuse(config, "my-app", sleep_fn=_never_sleep)
        self.launch_stderr = stderr.getvalue()
        return fake_proc

    def test_launch_records_listener_pid_in_registry_and_state(self):
        self._launch(listener_pid=999)

        entries = browser._read_registry(self.registry_path)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["pid"], 42)
        self.assertEqual(entries[0]["listenerPid"], 999)
        self.assertEqual(browser._state["my-app"]["listenerPid"], 999)

    def test_launch_records_none_when_listener_pid_unresolved(self):
        self._launch(listener_pid=None)

        entries = browser._read_registry(self.registry_path)
        self.assertIsNone(entries[0]["listenerPid"])
        self.assertIsNone(browser._state["my-app"]["listenerPid"])
        # The fallback is silent in the registry but not to the user:
        # it warns on stderr that cleanup can only track the wrapper.
        self.assertIn("could not resolve the real listening pid", self.launch_stderr)
        self.assertIn("my-app", self.launch_stderr)

    def test_launch_says_nothing_when_listener_pid_resolves(self):
        self._launch(listener_pid=999)

        self.assertEqual(self.launch_stderr, "")

    def test_cleanup_kills_listener_pid_when_it_still_matches(self):
        fake_proc = self._launch(listener_pid=999, wrapper_pid=42)

        with mock.patch.object(server, "process_cmdline", return_value="backlog browser --port 6421") as cmdline, \
             mock.patch.object(server, "kill_process") as kill:
            browser._cleanup_all()

        self.assertTrue(fake_proc.terminated)  # wrapper: terminated directly via its Popen handle
        cmdline.assert_called_once_with(999)
        kill.assert_called_once_with(999)  # listener: guarded kill, since it's just a pid number

    def test_cleanup_does_not_kill_listener_pid_if_reused_by_something_else(self):
        fake_proc = self._launch(listener_pid=999, wrapper_pid=42)

        with mock.patch.object(server, "process_cmdline", return_value="/usr/bin/some-other-daemon"), \
             mock.patch.object(server, "kill_process") as kill:
            browser._cleanup_all()

        self.assertTrue(fake_proc.terminated)  # the wrapper is still terminated regardless
        kill.assert_not_called()  # but the (no-longer-matching) listener pid is left alone

    def test_cleanup_skips_the_guard_entirely_when_listener_pid_was_never_resolved(self):
        self._launch(listener_pid=None)

        with mock.patch.object(server, "process_cmdline") as cmdline, \
             mock.patch.object(server, "kill_process") as kill:
            browser._cleanup_all()

        cmdline.assert_not_called()
        kill.assert_not_called()

    def test_sweep_kills_both_wrapper_and_listener_when_both_still_match(self):
        browser._write_registry(self.registry_path, [
            {"pid": 111, "listenerPid": 222, "port": 6421, "project": "my-app", "startTime": 0},
        ])

        def fake_cmdline(pid):
            return "backlog browser --port 6421" if pid in (111, 222) else None

        with mock.patch.object(server, "process_cmdline", side_effect=fake_cmdline), \
             mock.patch.object(server, "kill_process") as kill:
            result = browser.sweep_orphaned_browsers()

        self.assertEqual(len(result["killed"]), 1)
        self.assertEqual(result["stale"], [])
        killed_pids = {c.args[0] for c in kill.call_args_list}
        self.assertEqual(killed_pids, {111, 222})

    def test_sweep_kills_listener_even_when_wrapper_pid_is_already_dead(self):
        browser._write_registry(self.registry_path, [
            {"pid": 111, "listenerPid": 222, "port": 6421, "project": "my-app", "startTime": 0},
        ])

        def fake_cmdline(pid):
            return "backlog browser --port 6421" if pid == 222 else None  # wrapper (111) already gone

        with mock.patch.object(server, "process_cmdline", side_effect=fake_cmdline), \
             mock.patch.object(server, "kill_process") as kill:
            result = browser.sweep_orphaned_browsers()

        self.assertEqual(len(result["killed"]), 1)  # the entry still counts as killed via the listener
        kill.assert_called_once_with(222)

    def test_sweep_entry_is_stale_only_when_neither_pid_matches(self):
        browser._write_registry(self.registry_path, [
            {"pid": 111, "listenerPid": 222, "port": 6421, "project": "my-app", "startTime": 0},
        ])

        with mock.patch.object(server, "process_cmdline", return_value=None), \
             mock.patch.object(server, "kill_process") as kill:
            result = browser.sweep_orphaned_browsers()

        self.assertEqual(result["killed"], [])
        self.assertEqual(len(result["stale"]), 1)
        kill.assert_not_called()


class BrowserOccupiedPortTests(unittest.TestCase):
    """Task-27: a foreign process squatting the assigned port must never
    get silently reused or handed out -- walk forward to a verified-free
    port instead, and drop --non-interactive there so a race that beats
    even the fallback fails loudly rather than drifting to some port
    nobody checked."""

    def setUp(self):
        browser._reset_state()
        self.addCleanup(browser._reset_state)
        self.registry_path = _use_temp_registry(self)
        _stub_resolve_listener_pid(self)

    def test_occupied_assigned_port_walks_forward_and_drops_non_interactive(self):
        config = make_config([{"name": "my-app", "path": "/repos/my-app", "browserPort": None}])

        # 6421 (assigned) occupied; 6422 free; then "taken" for the
        # post-launch verification poll.
        port_checks = iter([False, True, False])

        def fake_port_is_free(port):
            return next(port_checks)

        with mock.patch.object(server, "launch_browser_process", return_value=FakeProc()) as launch, \
             mock.patch.object(server, "port_is_free", side_effect=fake_port_is_free):
            result = browser.launch_or_reuse(config, "my-app", sleep_fn=_never_sleep)

        self.assertEqual(result, {"url": "http://127.0.0.1:6422"})
        launch.assert_called_once_with(
            ["backlog", "browser", "--port", "6422", "--no-open"],  # no --non-interactive
            cwd="/repos/my-app",
        )

    def test_walks_forward_past_multiple_occupied_ports(self):
        config = make_config([{"name": "my-app", "path": "/repos/my-app", "browserPort": None}])
        # 6421, 6422, 6423 occupied; 6424 free; then "taken" for verification.
        port_checks = iter([False, False, False, True, False])

        def fake_port_is_free(port):
            return next(port_checks)

        with mock.patch.object(server, "launch_browser_process", return_value=FakeProc()) as launch, \
             mock.patch.object(server, "port_is_free", side_effect=fake_port_is_free):
            result = browser.launch_or_reuse(config, "my-app", sleep_fn=_never_sleep)

        self.assertEqual(result, {"url": "http://127.0.0.1:6424"})
        (argv,), _ = launch.call_args
        self.assertIn("6424", argv)

    def test_port_range_exhausted_raises_clear_browser_error(self):
        config = make_config([{"name": "my-app", "path": "/repos/my-app", "browserPort": None}])
        with mock.patch.object(server, "launch_browser_process") as launch, \
             mock.patch.object(server, "port_is_free", return_value=False):  # nothing is ever free
            with self.assertRaises(browser.BrowserError) as ctx:
                browser.launch_or_reuse(config, "my-app", sleep_fn=_never_sleep)
        self.assertEqual(ctx.exception.status, 500)
        self.assertIn("no free port", str(ctx.exception))
        launch.assert_not_called()

    def test_existing_tracked_process_on_the_assigned_port_is_still_reused(self):
        # The reuse fast-path (already-tracked, still-alive, same port)
        # must take priority over the bind-check entirely -- it's OUR
        # process holding that port, not a foreign one.
        config = make_config([{"name": "my-app", "path": "/repos/my-app", "browserPort": None}])
        with mock.patch.object(server, "launch_browser_process", return_value=FakeProc(alive=True)) as launch, \
             mock.patch.object(server, "port_is_free", side_effect=_free_then_taken()) as port_is_free:
            browser.launch_or_reuse(config, "my-app", sleep_fn=_never_sleep)
            port_is_free.reset_mock()
            result = browser.launch_or_reuse(config, "my-app", sleep_fn=_never_sleep)

        self.assertEqual(result, {"url": "http://127.0.0.1:6421"})
        launch.assert_called_once()
        port_is_free.assert_not_called()  # reuse never even checks the port


class BrowserLaunchVerificationTests(unittest.TestCase):
    """Task-27: a live Popen handle alone isn't proof the child is
    actually serving -- _verify_launch (via launch_or_reuse) must catch
    an immediate crash or a hang that never starts listening, and report
    a clear error instead of handing out a URL nothing answers on."""

    def setUp(self):
        browser._reset_state()
        self.addCleanup(browser._reset_state)
        self.registry_path = _use_temp_registry(self)
        _stub_resolve_listener_pid(self)

    def test_child_that_exits_immediately_raises_clear_error(self):
        config = make_config([{"name": "my-app", "path": "/repos/my-app", "browserPort": None}])
        dead_proc = FakeProc(alive=False)
        with mock.patch.object(server, "launch_browser_process", return_value=dead_proc), \
             mock.patch.object(server, "port_is_free", return_value=True):  # always free -- nothing ever binds
            with self.assertRaises(browser.BrowserError) as ctx:
                browser.launch_or_reuse(config, "my-app", sleep_fn=_never_sleep)
        self.assertEqual(ctx.exception.status, 500)
        self.assertIn("exited immediately", str(ctx.exception))
        # A dead process was never tracked as this project's live browser.
        self.assertNotIn("my-app", browser._state)

    def test_port_never_becomes_occupied_times_out_and_terminates_child(self):
        config = make_config([{"name": "my-app", "path": "/repos/my-app", "browserPort": None}])
        hung_proc = FakeProc(alive=True)  # alive, but never actually binds the port
        sleep_calls = {"n": 0}

        def counting_sleep(seconds):
            sleep_calls["n"] += 1

        with mock.patch.object(server, "launch_browser_process", return_value=hung_proc), \
             mock.patch.object(server, "port_is_free", return_value=True):  # always free -- it never bound
            with self.assertRaises(browser.BrowserError) as ctx:
                browser.launch_or_reuse(config, "my-app", sleep_fn=counting_sleep)

        self.assertEqual(ctx.exception.status, 500)
        self.assertIn("did not start listening", str(ctx.exception))
        self.assertTrue(hung_proc.terminated)
        self.assertGreater(sleep_calls["n"], 0)
        self.assertNotIn("my-app", browser._state)

    def test_verification_polls_multiple_times_before_succeeding(self):
        config = make_config([{"name": "my-app", "path": "/repos/my-app", "browserPort": None}])
        proc = FakeProc(alive=True)
        # Free (pre-launch), then still free for two verification polls,
        # then finally taken on the third -- proves the poll loop retries
        # rather than only checking once.
        port_checks = iter([True, True, True, False])

        def fake_port_is_free(port):
            return next(port_checks)

        sleep_calls = {"n": 0}

        def counting_sleep(seconds):
            sleep_calls["n"] += 1

        with mock.patch.object(server, "launch_browser_process", return_value=proc), \
             mock.patch.object(server, "port_is_free", side_effect=fake_port_is_free):
            result = browser.launch_or_reuse(config, "my-app", sleep_fn=counting_sleep)

        self.assertEqual(result, {"url": "http://127.0.0.1:6421"})
        self.assertEqual(sleep_calls["n"], 2)  # polled, slept, polled, slept, polled -- success on the 3rd


class BrowserRegistryPathTests(unittest.TestCase):
    """registry_path() itself -- the one piece of task-28's registry work
    that isn't mocked away in every other test class (they redirect it
    to a temp file via _use_temp_registry instead)."""

    def test_defaults_under_home_cache(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("XDG_CACHE_HOME", None)
            path = browser.registry_path()
        self.assertEqual(path, os.path.join(os.path.expanduser("~/.cache"), "centrale", "browsers.json"))

    def test_honors_xdg_cache_home(self):
        with mock.patch.dict(os.environ, {"XDG_CACHE_HOME": "/tmp/xdg-cache-test"}):
            path = browser.registry_path()
        self.assertEqual(path, "/tmp/xdg-cache-test/centrale/browsers.json")


class BrowserRegistryReadWriteTests(unittest.TestCase):
    """Hermetic tests for the registry file itself: read/write round-trip,
    tolerance of a missing/corrupt file, and that a launch/cleanup cycle
    keeps it in sync -- all against a throwaway temp path, never the
    user's real ~/.cache/centrale/browsers.json."""

    def setUp(self):
        browser._reset_state()
        self.addCleanup(browser._reset_state)
        self.registry_path = _use_temp_registry(self)
        _stub_resolve_listener_pid(self)

    def test_read_registry_missing_file_returns_empty_list(self):
        self.assertEqual(browser._read_registry(self.registry_path), [])

    def test_read_registry_corrupt_json_returns_empty_list(self):
        os.makedirs(os.path.dirname(self.registry_path), exist_ok=True)
        with open(self.registry_path, "w", encoding="utf-8") as f:
            f.write("{not valid json")
        self.assertEqual(browser._read_registry(self.registry_path), [])

    def test_read_registry_ignores_malformed_entries(self):
        os.makedirs(os.path.dirname(self.registry_path), exist_ok=True)
        with open(self.registry_path, "w", encoding="utf-8") as f:
            json.dump([{"pid": 123, "port": 6421}, {"no": "pid here"}, "not even a dict", 42], f)
        entries = browser._read_registry(self.registry_path)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["pid"], 123)

    def test_write_then_read_round_trips(self):
        browser._write_registry(self.registry_path, [{"pid": 1, "port": 6421, "project": "my-app"}])
        self.assertEqual(browser._read_registry(self.registry_path), [
            {"pid": 1, "port": 6421, "project": "my-app"},
        ])

    def test_write_registry_is_atomic_no_temp_file_left_behind(self):
        browser._write_registry(self.registry_path, [{"pid": 1, "port": 6421}])
        directory = os.path.dirname(self.registry_path)
        leftover = [f for f in os.listdir(directory) if f.startswith(".browsers-") and f.endswith(".json.tmp")]
        self.assertEqual(leftover, [])

    def test_launch_records_a_registry_entry(self):
        config = make_config([{"name": "my-app", "path": "/repos/my-app", "browserPort": None}])
        fake_proc = FakeProc(pid=555, alive=True)
        with mock.patch.object(server, "launch_browser_process", return_value=fake_proc), \
             mock.patch.object(server, "port_is_free", side_effect=_free_then_taken()):
            browser.launch_or_reuse(config, "my-app", sleep_fn=_never_sleep)

        entries = browser._read_registry(self.registry_path)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["pid"], 555)
        self.assertEqual(entries[0]["port"], 6421)
        self.assertEqual(entries[0]["project"], "my-app")
        self.assertIn("startTime", entries[0])

    def test_cleanup_removes_the_registry_entry(self):
        config = make_config([{"name": "my-app", "path": "/repos/my-app", "browserPort": None}])
        fake_proc = FakeProc(pid=555, alive=True)
        with mock.patch.object(server, "launch_browser_process", return_value=fake_proc), \
             mock.patch.object(server, "port_is_free", side_effect=_free_then_taken()):
            browser.launch_or_reuse(config, "my-app", sleep_fn=_never_sleep)

        self.assertEqual(len(browser._read_registry(self.registry_path)), 1)
        browser._cleanup_all()
        self.assertEqual(browser._read_registry(self.registry_path), [])


class BrowserBootSweepTests(unittest.TestCase):
    """Hermetic tests for sweep_orphaned_browsers()'s decision matrix:
    dead pid -> stale (drop, no kill); alive but pid reused by something
    else -> stale (drop, no kill -- never kill on pid alone); alive and
    still a `backlog browser` process -> killed. server.process_cmdline
    and server.kill_process are the only boundaries touched, both
    mocked -- no real process is ever inspected or signaled."""

    def setUp(self):
        self.registry_path = _use_temp_registry(self)

    def _seed(self, entries):
        browser._write_registry(self.registry_path, entries)

    def test_dead_pid_is_dropped_without_killing(self):
        self._seed([{"pid": 111, "port": 6421, "project": "my-app", "startTime": 0}])
        with mock.patch.object(server, "process_cmdline", return_value=None) as cmdline, \
             mock.patch.object(server, "kill_process") as kill:
            result = browser.sweep_orphaned_browsers()

        self.assertEqual(result["killed"], [])
        self.assertEqual(len(result["stale"]), 1)
        cmdline.assert_called_once_with(111)
        kill.assert_not_called()
        self.assertEqual(browser._read_registry(self.registry_path), [])

    def test_alive_but_pid_reused_by_unrelated_process_is_not_killed(self):
        self._seed([{"pid": 222, "port": 6422, "project": "my-app", "startTime": 0}])
        with mock.patch.object(server, "process_cmdline", return_value="/usr/bin/some-other-daemon --flag"), \
             mock.patch.object(server, "kill_process") as kill:
            result = browser.sweep_orphaned_browsers()

        self.assertEqual(result["killed"], [])
        self.assertEqual(len(result["stale"]), 1)
        kill.assert_not_called()

    def test_alive_and_still_backlog_browser_is_killed(self):
        self._seed([{"pid": 333, "port": 6423, "project": "my-app", "startTime": 0}])
        with mock.patch.object(
            server, "process_cmdline",
            return_value="backlog browser --port 6423 --no-open --non-interactive",
        ), mock.patch.object(server, "kill_process") as kill:
            result = browser.sweep_orphaned_browsers()

        self.assertEqual(len(result["killed"]), 1)
        self.assertEqual(result["stale"], [])
        kill.assert_called_once_with(333)

    def test_registry_is_cleared_after_a_sweep_regardless_of_outcome(self):
        self._seed([
            {"pid": 111, "port": 6421, "project": "my-app", "startTime": 0},
            {"pid": 222, "port": 6422, "project": "my-tool", "startTime": 0},
        ])

        def fake_cmdline(pid):
            return "backlog browser --port 6421" if pid == 111 else None

        with mock.patch.object(server, "process_cmdline", side_effect=fake_cmdline), \
             mock.patch.object(server, "kill_process"):
            browser.sweep_orphaned_browsers()

        self.assertEqual(browser._read_registry(self.registry_path), [])

    def test_empty_registry_sweeps_nothing(self):
        with mock.patch.object(server, "process_cmdline") as cmdline, \
             mock.patch.object(server, "kill_process") as kill:
            result = browser.sweep_orphaned_browsers()

        self.assertEqual(result, {"killed": [], "stale": []})
        cmdline.assert_not_called()
        kill.assert_not_called()

    # -- task-105: the same matrix on a machine with no procfs --------

    def test_orphan_is_reaped_on_a_machine_with_no_procfs(self):
        # macOS: server._cmdline_from_procfs can never answer, so the
        # sweep is only as good as the `ps` fallback. Exercised here by
        # stubbing the procfs half to None (what it does with no /proc)
        # and mocking subprocess.run for the `ps` half -- the real
        # server.process_cmdline chain runs in between. Before task-105
        # this entry survived every sweep, forever.
        self._seed([{"pid": 555, "port": 6425, "project": "my-app", "startTime": 0}])
        ps = subprocess.CompletedProcess([], 0, "node /usr/lib/backlog/cli.js browser --port 6425\n", "")
        with mock.patch.object(server, "_cmdline_from_procfs", return_value=None), \
             mock.patch("subprocess.run", return_value=ps), \
             mock.patch.object(server, "kill_process") as kill:
            result = browser.sweep_orphaned_browsers()

        self.assertEqual(len(result["killed"]), 1)
        kill.assert_called_once_with(555)

    def test_nothing_is_killed_when_no_method_can_identify_the_pid(self):
        # The fail-closed rule, end to end: no procfs *and* no usable
        # `ps` (a stripped container, say) means the pid stays
        # unidentified -- so it is dropped from the registry, never
        # signaled. Killing on pid alone is exactly what must not happen.
        self._seed([{"pid": 666, "port": 6426, "project": "my-app", "startTime": 0}])
        with mock.patch.object(server, "_cmdline_from_procfs", return_value=None), \
             mock.patch("subprocess.run", side_effect=FileNotFoundError("no ps")), \
             mock.patch.object(server, "kill_process") as kill:
            result = browser.sweep_orphaned_browsers()

        self.assertEqual(result["killed"], [])
        self.assertEqual(len(result["stale"]), 1)
        kill.assert_not_called()
        self.assertEqual(browser._read_registry(self.registry_path), [])


def _proc(returncode=0, stdout=""):
    """A CompletedProcess stand-in for run_backlog_raw, which never
    raises on failure -- a missing/failing/timing-out `backlog` arrives
    as a non-zero return code (see server._run)."""
    return subprocess.CompletedProcess(args=["backlog", "--version"], returncode=returncode,
                                       stdout=stdout, stderr="")


class BrowserVersionProbeTests(unittest.TestCase):
    """The two boundaries version_drift compares (task-157), each on its
    own: what a board answers on /api/version, and what `backlog
    --version` prints. Both degrade to None rather than raising -- a
    version that cannot be established is silence, not a warning."""

    def test_probe_answers_none_when_nothing_listens(self):
        # The real boundary against a closed loopback port: a refused
        # connection is "no board", not an exception out of an open.
        import socket
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            free_port = sock.getsockname()[1]
        self.assertIsNone(server.probe_browser_version(free_port))

    def _serve_once(self, body, content_type="application/json"):
        """A one-request loopback HTTP server, on an ephemeral port it
        picks itself (nothing here assumes a port is free -- parallel
        agents share this machine). Returns the port."""
        import http.server

        class Once(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                payload = body.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):  # keep the runner's output clean
                pass

        httpd = http.server.HTTPServer(("127.0.0.1", 0), Once)
        self.addCleanup(httpd.server_close)
        thread = threading.Thread(target=httpd.handle_request, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        return httpd.server_address[1]

    def test_probe_reads_the_version_a_board_reports(self):
        port = self._serve_once('{"version":"1.50.1"}')
        self.assertEqual(server.probe_browser_version(port), "1.50.1")

    def test_probe_answers_none_for_an_answer_that_is_not_a_board(self):
        port = self._serve_once("<html>not a board</html>", content_type="text/html")
        self.assertIsNone(server.probe_browser_version(port))

    def test_probe_answers_none_when_the_json_carries_no_version(self):
        port = self._serve_once('{"tasks":[]}')
        self.assertIsNone(server.probe_browser_version(port))

    def test_cli_version_is_what_backlog_prints(self):
        with mock.patch.object(server, "run_backlog_raw", return_value=_proc(stdout="1.51.0\n")) as run:
            self.assertEqual(server.backlog_cli_version(), "1.51.0")
        self.assertEqual(run.call_args.args[0], ["--version"])

    def test_cli_version_is_none_when_backlog_cannot_be_asked(self):
        with mock.patch.object(server, "run_backlog_raw", return_value=_proc(returncode=127)):
            self.assertIsNone(server.backlog_cli_version())

    def test_cli_version_is_none_when_backlog_prints_nothing(self):
        with mock.patch.object(server, "run_backlog_raw", return_value=_proc(stdout="  \n")):
            self.assertIsNone(server.backlog_cli_version())

    def test_both_sides_reduce_to_the_bare_number(self):
        # The two sides are the same package asked two ways and normally
        # print the number identically; this only keeps a difference in
        # how one of them chooses to print it from reading as a version
        # difference (a false "your board is stale" would teach the
        # reader to ignore the true one).
        port = self._serve_once('{"version":"backlog 1.51.0"}')
        self.assertEqual(server.probe_browser_version(port), "1.51.0")
        with mock.patch.object(server, "run_backlog_raw", return_value=_proc(stdout="backlog 1.51.0\n")):
            self.assertEqual(server.backlog_cli_version(), "1.51.0")

    def test_both_sides_reject_the_same_malformed_versions(self):
        # task-162: probe_browser_version and backlog_cli_version share
        # backlog_version()'s validation, so a malformed, partial,
        # overlong, empty, or non-string version reads as None on either
        # side rather than leaking through as a literal, unvalidated
        # token -- exactly the shapes BacklogVersionParsingTests in
        # tests/test_doctor.py already proves the doctor side rejects.
        for reported in ('"not a version"', '"1.51"', '"1.51.0.2"', '""', "123"):
            with self.subTest(reported=reported):
                port = self._serve_once('{"version":%s}' % reported)
                self.assertIsNone(server.probe_browser_version(port))
        for stdout in ("not a version\n", "1.51\n", "1.51.0.2\n", "  \n"):
            with self.subTest(stdout=stdout):
                with mock.patch.object(server, "run_backlog_raw", return_value=_proc(stdout=stdout)):
                    self.assertIsNone(server.backlog_cli_version())


class BrowserVersionDriftTests(unittest.TestCase):
    """version_drift(): a board Centrale launched keeps the version it
    started with, and an upgrade on disk does not touch it (task-157).
    Every boundary is mocked -- no board is ever contacted and no
    `backlog` is ever run -- and every test asserts nothing was signaled,
    because reporting a difference must never become acting on one."""

    def setUp(self):
        browser._reset_state()
        self.addCleanup(browser._reset_state)

    def _track(self, project, port):
        """Record a live board for `project` the way a successful launch
        does, without launching one."""
        browser._state[project] = {"proc": FakeProc(pid=1, alive=True), "port": port,
                                   "listenerPid": None}

    def test_a_board_behind_the_cli_is_reported_with_both_versions(self):
        self._track("my-app", 6421)
        with mock.patch.object(server, "probe_browser_version", return_value="1.50.1") as probe, \
             mock.patch.object(server, "backlog_cli_version", return_value="1.51.0"), \
             mock.patch.object(server, "kill_process") as kill:
            drift = browser.version_drift("my-app")

        self.assertEqual(drift, {"running": "1.50.1", "cli": "1.51.0"})
        probe.assert_called_once_with(6421)  # the port the board was tracked on
        kill.assert_not_called()

    def test_nothing_is_killed_or_restarted_on_a_difference(self):
        # The explicit form of the criterion: Centrale launched the
        # process, but the user may be reading it, so a version
        # difference is reported and nothing else happens to it.
        self._track("my-app", 6421)
        proc = browser._state["my-app"]["proc"]
        with mock.patch.object(server, "probe_browser_version", return_value="1.50.1"), \
             mock.patch.object(server, "backlog_cli_version", return_value="1.51.0"), \
             mock.patch.object(server, "kill_process") as kill, \
             mock.patch.object(server, "launch_browser_process") as launch:
            browser.version_drift("my-app")

        kill.assert_not_called()
        launch.assert_not_called()
        self.assertFalse(proc.terminated)
        # And the board stays tracked exactly as it was.
        self.assertEqual(browser._state["my-app"]["port"], 6421)

    def test_agreement_is_silence(self):
        self._track("my-app", 6421)
        with mock.patch.object(server, "probe_browser_version", return_value="1.51.0"), \
             mock.patch.object(server, "backlog_cli_version", return_value="1.51.0"):
            self.assertIsNone(browser.version_drift("my-app"))

    def test_no_tracked_board_asks_nothing(self):
        with mock.patch.object(server, "probe_browser_version") as probe, \
             mock.patch.object(server, "backlog_cli_version") as cli:
            self.assertIsNone(browser.version_drift("my-app"))
        probe.assert_not_called()
        cli.assert_not_called()

    def test_a_board_that_does_not_answer_is_silence_and_the_cli_is_not_run(self):
        self._track("my-app", 6421)
        with mock.patch.object(server, "probe_browser_version", return_value=None), \
             mock.patch.object(server, "backlog_cli_version") as cli:
            self.assertIsNone(browser.version_drift("my-app"))
        cli.assert_not_called()

    def test_a_backlog_that_cannot_be_asked_is_silence(self):
        self._track("my-app", 6421)
        with mock.patch.object(server, "probe_browser_version", return_value="1.50.1"), \
             mock.patch.object(server, "backlog_cli_version", return_value=None):
            self.assertIsNone(browser.version_drift("my-app"))

    def test_the_probed_port_is_the_one_a_real_launch_recorded(self):
        # End to end through launch_or_reuse, including the walked-forward
        # port: version_drift must ask the board that was actually
        # launched, not the port it was assigned.
        _use_temp_registry(self)
        _stub_resolve_listener_pid(self)
        config = make_config([{"name": "my-app", "path": "/repos/my-app", "browserPort": None}])
        # 6421 taken, 6422 free, then taken (the launch verification).
        with mock.patch.object(server, "launch_browser_process", return_value=FakeProc(pid=7)), \
             mock.patch.object(server, "port_is_free", side_effect=[False, True, False]):
            result = browser.launch_or_reuse(config, "my-app", sleep_fn=_never_sleep)
        self.assertEqual(result, {"url": "http://127.0.0.1:6422"})

        with mock.patch.object(server, "probe_browser_version", return_value="1.50.1") as probe, \
             mock.patch.object(server, "backlog_cli_version", return_value="1.51.0"):
            drift = browser.version_drift("my-app")
        probe.assert_called_once_with(6422)
        self.assertEqual(drift, {"running": "1.50.1", "cli": "1.51.0"})


class BrowserRegisterTimeReconcileTests(unittest.TestCase):
    """Registering a launch drops the project's own dead rows first
    (task-157), so relaunching a board in one session leaves one entry
    rather than a dead one beside the live one. Pruning decides by the
    same cmdline-confirming guard the sweep does, and kills nothing --
    every test below asserts server.kill_process was never called."""

    def setUp(self):
        browser._reset_state()
        self.addCleanup(browser._reset_state)
        self.registry_path = _use_temp_registry(self)
        _stub_resolve_listener_pid(self)

    def _launch(self, config, project, pid):
        with mock.patch.object(server, "launch_browser_process", return_value=FakeProc(pid=pid)), \
             mock.patch.object(server, "port_is_free", side_effect=_free_then_taken()):
            return browser.launch_or_reuse(config, project, sleep_fn=_never_sleep)

    def test_relaunch_in_one_session_leaves_exactly_one_entry(self):
        # The observed shape: a board is opened, its process is killed
        # from outside Centrale, and the board is opened again. The
        # registry held both the dead pid and the live one until the next
        # restart swept it.
        config = make_config([{"name": "my-app", "path": "/repos/my-app", "browserPort": None}])
        with mock.patch.object(server, "kill_process") as kill:
            self._launch(config, "my-app", pid=4100237)
            self.assertEqual([e["pid"] for e in browser._read_registry(self.registry_path)],
                             [4100237])

            # The first process is gone: nothing answers for its pid.
            browser._reset_state()
            with mock.patch.object(server, "process_cmdline", return_value=None):
                self._launch(config, "my-app", pid=652156)

            entries = browser._read_registry(self.registry_path)

        self.assertEqual([e["pid"] for e in entries], [652156])
        self.assertEqual([e["project"] for e in entries], ["my-app"])
        kill.assert_not_called()

    def test_a_still_live_entry_for_the_same_project_is_kept(self):
        # Two live boards for one project is a shape the sweep should
        # see, not one to forget: only DEAD rows are dropped.
        config = make_config([{"name": "my-app", "path": "/repos/my-app", "browserPort": None}])
        with mock.patch.object(server, "kill_process") as kill:
            self._launch(config, "my-app", pid=111)
            browser._reset_state()
            with mock.patch.object(server, "process_cmdline",
                                   return_value="node /usr/bin/backlog browser --port 6421"):
                self._launch(config, "my-app", pid=222)
            entries = browser._read_registry(self.registry_path)

        self.assertEqual([e["pid"] for e in entries], [111, 222])
        kill.assert_not_called()

    def test_pruning_uses_the_cmdline_guard_not_liveness_alone(self):
        # The pid is alive, but it belongs to something else now -- the
        # same reuse the sweep refuses to kill on. The row describes a
        # browser that is gone, so it is dropped; nothing is signaled.
        config = make_config([{"name": "my-app", "path": "/repos/my-app", "browserPort": None}])
        with mock.patch.object(server, "kill_process") as kill:
            self._launch(config, "my-app", pid=111)
            browser._reset_state()
            with mock.patch.object(server, "process_cmdline",
                                   return_value="/usr/bin/postgres -D /var/lib/pg") as cmdline:
                self._launch(config, "my-app", pid=222)
            entries = browser._read_registry(self.registry_path)

        self.assertEqual([e["pid"] for e in entries], [222])
        cmdline.assert_any_call(111)  # the guard was consulted
        kill.assert_not_called()

    def test_an_entry_is_live_if_either_of_its_pids_still_matches(self):
        # A row carries the wrapper pid and, when it could be resolved,
        # the real listener's. The wrapper exiting does not make the row
        # dead while the listener underneath it is still serving.
        browser._write_registry(self.registry_path, [
            {"pid": 111, "listenerPid": 112, "port": 6421, "project": "my-app", "startTime": 0},
        ])
        config = make_config([{"name": "my-app", "path": "/repos/my-app", "browserPort": None}])
        alive = {112: "node /usr/bin/backlog browser --port 6421"}
        with mock.patch.object(server, "process_cmdline", side_effect=lambda pid: alive.get(pid)), \
             mock.patch.object(server, "kill_process") as kill:
            self._launch(config, "my-app", pid=222)
            entries = browser._read_registry(self.registry_path)

        self.assertEqual([e["pid"] for e in entries], [111, 222])
        kill.assert_not_called()

    def test_dead_entries_for_other_projects_are_left_alone(self):
        # Reconciliation is scoped to the project being registered: a
        # dead row for another project is the boot sweep's business, not
        # this launch's.
        browser._write_registry(self.registry_path, [
            {"pid": 111, "port": 6421, "project": "my-tool", "startTime": 0},
        ])
        config = make_config([{"name": "my-app", "path": "/repos/my-app", "browserPort": None}])
        with mock.patch.object(server, "process_cmdline", return_value=None), \
             mock.patch.object(server, "kill_process") as kill:
            self._launch(config, "my-app", pid=222)
            entries = browser._read_registry(self.registry_path)

        self.assertEqual([(e["project"], e["pid"]) for e in entries],
                         [("my-tool", 111), ("my-app", 222)])
        kill.assert_not_called()


class BrowserHttpApiTests(unittest.TestCase):
    """End-to-end POST /api/browser tests against a real ThreadingHTTPServer,
    with browser.launch_or_reuse mocked so no real process is ever started."""

    @classmethod
    def setUpClass(cls):
        cls.config = make_config([{"name": "my-app", "path": "/repos/my-app", "browserPort": None}])
        cls.httpd = server.CentraleHTTPServer(("127.0.0.1", 0), server.Handler, cls.config)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join(timeout=5)

    def _url(self, path):
        return f"http://127.0.0.1:{self.port}{path}"

    def _post(self, path, body_bytes, headers=None):
        req = urllib.request.Request(
            self._url(path), data=body_bytes, method="POST",
            headers=headers or {"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_post_browser_success(self):
        payload = json.dumps({"project": "my-app"}).encode("utf-8")
        with mock.patch("browser.launch_or_reuse", return_value={"url": "http://127.0.0.1:6421"}) as fake, \
             mock.patch("browser.version_drift", return_value=None):
            status, body = self._post("/api/browser", payload)

        self.assertEqual(status, 200)
        self.assertEqual(body, {"url": "http://127.0.0.1:6421", "versionDrift": None})
        fake.assert_called_once_with(self.config, "my-app")

    def test_post_browser_reports_a_stale_board_beside_its_url(self):
        # task-157: the drift is asked for AFTER the launch/reuse, and
        # about the same project -- so what it describes is the board the
        # caller is about to open, not some other one.
        payload = json.dumps({"project": "my-app"}).encode("utf-8")
        drift = {"running": "1.50.1", "cli": "1.51.0"}
        with mock.patch("browser.launch_or_reuse", return_value={"url": "http://127.0.0.1:6421"}), \
             mock.patch("browser.version_drift", return_value=drift) as fake:
            status, body = self._post("/api/browser", payload)

        self.assertEqual(status, 200)
        self.assertEqual(body["url"], "http://127.0.0.1:6421")
        self.assertEqual(body["versionDrift"], drift)
        fake.assert_called_once_with("my-app")

    def test_post_browser_failure_reports_no_drift_at_all(self):
        # Nothing was opened, so there is no board to describe: the error
        # path never asks.
        import browser as browser_module

        payload = json.dumps({"project": "my-app"}).encode("utf-8")
        with mock.patch("browser.launch_or_reuse",
                        side_effect=browser_module.BrowserError("boom", status=500)), \
             mock.patch("browser.version_drift") as fake:
            status, body = self._post("/api/browser", payload)

        self.assertEqual(status, 500)
        self.assertNotIn("versionDrift", body)
        fake.assert_not_called()

    def test_post_browser_surfaces_browser_error_status(self):
        import browser as browser_module

        payload = json.dumps({"project": "nonexistent"}).encode("utf-8")
        with mock.patch(
            "browser.launch_or_reuse",
            side_effect=browser_module.BrowserError("unknown project: nonexistent", status=404),
        ):
            status, body = self._post("/api/browser", payload)

        self.assertEqual(status, 404)
        self.assertIn("error", body)

    def test_post_browser_malformed_json_returns_400(self):
        status, body = self._post("/api/browser", b"{not valid json")
        self.assertEqual(status, 400)
        self.assertIn("error", body)

    def test_post_browser_empty_body_returns_400(self):
        status, body = self._post("/api/browser", b"")
        self.assertEqual(status, 400)
        self.assertIn("error", body)


if __name__ == "__main__":
    unittest.main()
