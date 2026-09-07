import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server  # noqa: E402


def _which_side_effect(available):
    """Builds a server.which stand-in: `available` is the set of binary
    names to report as present; everything else reports missing."""
    def _which(name):
        return f"/usr/bin/{name}" if name in available else None
    return _which


def _proc(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


class DoctorCheckTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)

    def _write_projects_json(self, data):
        path = os.path.join(self.tmpdir.name, "projects.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f)
        return path

    def _make_project_dir(self, name, with_backlog_config=True):
        path = os.path.join(self.tmpdir.name, name)
        os.makedirs(path, exist_ok=True)
        if with_backlog_config:
            os.makedirs(os.path.join(path, "backlog"), exist_ok=True)
            with open(os.path.join(path, "backlog", "config.yml"), "w") as f:
                f.write("statuses: [To Do, Done]\n")
        return path

    def test_all_green_when_everything_present_and_configured(self):
        repo = self._make_project_dir("my-app")
        config_path = self._write_projects_json({"projects": [{"name": "my-app", "path": repo}]})

        with mock.patch.object(server, "which", side_effect=_which_side_effect({"git", "backlog", "tmux"})), \
             mock.patch.object(server, "run_git", return_value=_proc(0, "git version 2.43.0\n")), \
             mock.patch.object(server, "run_backlog_raw", return_value=_proc(0, "backlog/1.50.1\n")):
            lines, ok = server.run_doctor_check(config_path=config_path)

        self.assertTrue(ok)
        joined = "\n".join(lines)
        self.assertIn("[PASS]", joined)
        self.assertNotIn("[FAIL]", joined)
        self.assertTrue(any("git version 2.43.0" in line for line in lines))
        self.assertTrue(any("backlog/1.50.1" in line for line in lines))
        self.assertTrue(any(line.startswith("[PASS]") and "my-app" in line for line in lines))

    def test_missing_git_is_a_hard_failure(self):
        repo = self._make_project_dir("my-app")
        config_path = self._write_projects_json({"projects": [{"name": "my-app", "path": repo}]})

        with mock.patch.object(server, "which", side_effect=_which_side_effect({"backlog", "tmux"})), \
             mock.patch.object(server, "run_backlog_raw", return_value=_proc(0, "backlog/1.50.1\n")):
            lines, ok = server.run_doctor_check(config_path=config_path)

        self.assertFalse(ok)
        self.assertTrue(any(line.startswith("[FAIL]") and "git" in line for line in lines))

    def test_missing_backlog_cli_is_a_hard_failure(self):
        repo = self._make_project_dir("my-app")
        config_path = self._write_projects_json({"projects": [{"name": "my-app", "path": repo}]})

        with mock.patch.object(server, "which", side_effect=_which_side_effect({"git", "tmux"})), \
             mock.patch.object(server, "run_git", return_value=_proc(0, "git version 2.43.0\n")):
            lines, ok = server.run_doctor_check(config_path=config_path)

        self.assertFalse(ok)
        self.assertTrue(any(line.startswith("[FAIL]") and "backlog" in line for line in lines))

    def test_missing_tmux_is_a_warning_not_a_failure(self):
        repo = self._make_project_dir("my-app")
        config_path = self._write_projects_json({"projects": [{"name": "my-app", "path": repo}]})

        with mock.patch.object(server, "which", side_effect=_which_side_effect({"git", "backlog"})), \
             mock.patch.object(server, "run_git", return_value=_proc(0, "git version 2.43.0\n")), \
             mock.patch.object(server, "run_backlog_raw", return_value=_proc(0, "backlog/1.50.1\n")):
            lines, ok = server.run_doctor_check(config_path=config_path)

        self.assertTrue(ok)  # tmux missing alone must not fail the whole check
        tmux_lines = [line for line in lines if "tmux" in line]
        self.assertTrue(any(line.startswith("[WARN]") for line in tmux_lines))
        # the "viewer-only" note the task spec calls for
        self.assertTrue(any("board" in line and "drawer" in line for line in tmux_lines))

    def test_old_python_version_is_a_hard_failure(self):
        repo = self._make_project_dir("my-app")
        config_path = self._write_projects_json({"projects": [{"name": "my-app", "path": repo}]})

        with mock.patch.object(server, "which", side_effect=_which_side_effect({"git", "backlog", "tmux"})), \
             mock.patch.object(server, "run_git", return_value=_proc(0, "git version 2.43.0\n")), \
             mock.patch.object(server, "run_backlog_raw", return_value=_proc(0, "backlog/1.50.1\n")), \
             mock.patch.object(server.sys, "version_info", (3, 10, 0, "final", 0)):
            lines, ok = server.run_doctor_check(config_path=config_path)

        self.assertFalse(ok)
        self.assertTrue(any(line.startswith("[FAIL]") and "Python" in line for line in lines))

    def test_malformed_projects_json_is_a_hard_failure(self):
        config_path = os.path.join(self.tmpdir.name, "bad.json")
        with open(config_path, "w") as f:
            f.write('{"agents": "not-an-object"}')  # normalize_agents_map raises ConfigError

        with mock.patch.object(server, "which", side_effect=_which_side_effect({"git", "backlog", "tmux"})), \
             mock.patch.object(server, "run_git", return_value=_proc(0, "git version 2.43.0\n")), \
             mock.patch.object(server, "run_backlog_raw", return_value=_proc(0, "backlog/1.50.1\n")):
            lines, ok = server.run_doctor_check(config_path=config_path)

        self.assertFalse(ok)
        self.assertTrue(any(line.startswith("[FAIL]") and "projects.json" in line for line in lines))
        # a broken config file is fatal before any per-project check even runs
        self.assertFalse(any("my-app" in line for line in lines))

    def test_missing_project_path_is_a_warning_not_a_failure(self):
        ghost = os.path.join(self.tmpdir.name, "does-not-exist")
        config_path = self._write_projects_json({"projects": [{"name": "ghost", "path": ghost}]})

        with mock.patch.object(server, "which", side_effect=_which_side_effect({"git", "backlog", "tmux"})), \
             mock.patch.object(server, "run_git", return_value=_proc(0, "git version 2.43.0\n")), \
             mock.patch.object(server, "run_backlog_raw", return_value=_proc(0, "backlog/1.50.1\n")):
            lines, ok = server.run_doctor_check(config_path=config_path)

        self.assertTrue(ok)  # one project's bad path degrades, doesn't block the server
        ghost_lines = [line for line in lines if "ghost" in line]
        self.assertTrue(any(line.startswith("[WARN]") for line in ghost_lines))

    def test_project_without_backlog_config_is_a_warning_not_a_failure(self):
        repo = self._make_project_dir("nobacklog", with_backlog_config=False)
        config_path = self._write_projects_json({"projects": [{"name": "nobacklog", "path": repo}]})

        with mock.patch.object(server, "which", side_effect=_which_side_effect({"git", "backlog", "tmux"})), \
             mock.patch.object(server, "run_git", return_value=_proc(0, "git version 2.43.0\n")), \
             mock.patch.object(server, "run_backlog_raw", return_value=_proc(0, "backlog/1.50.1\n")):
            lines, ok = server.run_doctor_check(config_path=config_path)

        self.assertTrue(ok)
        proj_lines = [line for line in lines if "nobacklog" in line]
        self.assertTrue(any(line.startswith("[WARN]") and "backlog init" in line for line in proj_lines))

    def test_no_projects_configured_is_a_warning_not_a_failure(self):
        config_path = self._write_projects_json({"projects": []})

        with mock.patch.object(server, "which", side_effect=_which_side_effect({"git", "backlog", "tmux"})), \
             mock.patch.object(server, "run_git", return_value=_proc(0, "git version 2.43.0\n")), \
             mock.patch.object(server, "run_backlog_raw", return_value=_proc(0, "backlog/1.50.1\n")):
            lines, ok = server.run_doctor_check(config_path=config_path)

        self.assertTrue(ok)
        self.assertTrue(any(line.startswith("[WARN]") and "no projects configured" in line for line in lines))

    def test_multiple_hard_failures_all_reported_not_just_the_first(self):
        config_path = self._write_projects_json({"projects": []})

        with mock.patch.object(server, "which", side_effect=_which_side_effect(set())):
            lines, ok = server.run_doctor_check(config_path=config_path)

        self.assertFalse(ok)
        fails = [line for line in lines if line.startswith("[FAIL]")]
        self.assertGreaterEqual(len(fails), 2)  # git and backlog both missing here
        self.assertTrue(any("git" in line for line in fails))
        self.assertTrue(any("backlog" in line for line in fails))

    def test_bwrap_not_on_path_is_silently_not_applicable(self):
        # No bwrap installed at all -- not a codex user, or not on a
        # bwrap-based distro -- must produce no line and no probe call,
        # not a warning.
        repo = self._make_project_dir("my-app")
        config_path = self._write_projects_json({"projects": [{"name": "my-app", "path": repo}]})

        with mock.patch.object(server, "which", side_effect=_which_side_effect({"git", "backlog", "tmux"})), \
             mock.patch.object(server, "run_git", return_value=_proc(0, "git version 2.43.0\n")), \
             mock.patch.object(server, "run_backlog_raw", return_value=_proc(0, "backlog/1.50.1\n")), \
             mock.patch.object(server, "run_bwrap_probe") as bwrap_probe, \
             mock.patch.object(server.sys, "platform", "linux"):
            lines, ok = server.run_doctor_check(config_path=config_path)

        self.assertTrue(ok)
        bwrap_probe.assert_not_called()
        self.assertFalse(any("bwrap" in line or "bubblewrap" in line for line in lines))

    def test_bwrap_probe_success_is_a_pass(self):
        repo = self._make_project_dir("my-app")
        config_path = self._write_projects_json({"projects": [{"name": "my-app", "path": repo}]})

        with mock.patch.object(server, "which", side_effect=_which_side_effect({"git", "backlog", "tmux", "bwrap"})), \
             mock.patch.object(server, "run_git", return_value=_proc(0, "git version 2.43.0\n")), \
             mock.patch.object(server, "run_backlog_raw", return_value=_proc(0, "backlog/1.50.1\n")), \
             mock.patch.object(server, "run_bwrap_probe", return_value=_proc(0)) as bwrap_probe, \
             mock.patch.object(server.sys, "platform", "linux"):
            lines, ok = server.run_doctor_check(config_path=config_path)

        self.assertTrue(ok)
        bwrap_probe.assert_called_once_with()
        self.assertTrue(any(line.startswith("[PASS]") and "bubblewrap" in line for line in lines))

    def test_bwrap_probe_failure_with_apparmor_restriction_gives_the_exact_warning(self):
        repo = self._make_project_dir("my-app")
        config_path = self._write_projects_json({"projects": [{"name": "my-app", "path": repo}]})

        with mock.patch.object(server, "which", side_effect=_which_side_effect({"git", "backlog", "tmux", "bwrap"})), \
             mock.patch.object(server, "run_git", return_value=_proc(0, "git version 2.43.0\n")), \
             mock.patch.object(server, "run_backlog_raw", return_value=_proc(0, "backlog/1.50.1\n")), \
             mock.patch.object(server, "run_bwrap_probe", return_value=_proc(1, "", "bwrap: setting up uid map: Permission denied\n")), \
             mock.patch.object(server, "apparmor_userns_restricted", return_value=True), \
             mock.patch.object(server.sys, "platform", "linux"):
            lines, ok = server.run_doctor_check(config_path=config_path)

        self.assertTrue(ok)  # non-fatal -- claude spawns are unaffected
        bwrap_lines = [line for line in lines if "bubblewrap" in line or "codex" in line]
        self.assertTrue(any(line.startswith("[WARN]") for line in bwrap_lines))
        self.assertTrue(any(
            "codex agents: bubblewrap sandbox blocked by AppArmor userns restriction" in line
            for line in bwrap_lines
        ))
        self.assertTrue(any("docs/operations.md troubleshooting" in line for line in bwrap_lines))

    def test_bwrap_probe_failure_without_apparmor_restriction_gives_a_generic_warning(self):
        repo = self._make_project_dir("my-app")
        config_path = self._write_projects_json({"projects": [{"name": "my-app", "path": repo}]})

        with mock.patch.object(server, "which", side_effect=_which_side_effect({"git", "backlog", "tmux", "bwrap"})), \
             mock.patch.object(server, "run_git", return_value=_proc(0, "git version 2.43.0\n")), \
             mock.patch.object(server, "run_backlog_raw", return_value=_proc(0, "backlog/1.50.1\n")), \
             mock.patch.object(server, "run_bwrap_probe", return_value=_proc(1, "", "some other unrelated failure\n")), \
             mock.patch.object(server, "apparmor_userns_restricted", return_value=False), \
             mock.patch.object(server.sys, "platform", "linux"):
            lines, ok = server.run_doctor_check(config_path=config_path)

        self.assertTrue(ok)
        bwrap_lines = [line for line in lines if "bubblewrap" in line]
        self.assertTrue(any(line.startswith("[WARN]") for line in bwrap_lines))
        self.assertTrue(any("some other unrelated failure" in line for line in bwrap_lines))
        # must not falsely claim the AppArmor-specific cause when it isn't the cause
        self.assertFalse(any("AppArmor" in line for line in bwrap_lines))

    def test_bwrap_check_skipped_entirely_on_non_linux_platforms(self):
        repo = self._make_project_dir("my-app")
        config_path = self._write_projects_json({"projects": [{"name": "my-app", "path": repo}]})

        with mock.patch.object(server, "which", side_effect=_which_side_effect({"git", "backlog", "tmux", "bwrap"})), \
             mock.patch.object(server, "run_git", return_value=_proc(0, "git version 2.43.0\n")), \
             mock.patch.object(server, "run_backlog_raw", return_value=_proc(0, "backlog/1.50.1\n")), \
             mock.patch.object(server, "run_bwrap_probe") as bwrap_probe, \
             mock.patch.object(server.sys, "platform", "darwin"):
            lines, ok = server.run_doctor_check(config_path=config_path)

        self.assertTrue(ok)
        bwrap_probe.assert_not_called()
        self.assertFalse(any("bwrap" in line or "bubblewrap" in line for line in lines))

    def test_apparmor_userns_restricted_reads_the_real_sysctl_file(self):
        # The one test that exercises apparmor_userns_restricted() itself
        # rather than mocking it -- a plain file read, safe to run for
        # real (never writes, never touches anything Centrale-specific).
        result = server.apparmor_userns_restricted()
        self.assertIn(result, (True, False, None))

    def test_never_calls_run_backlog_or_run_git_when_binaries_are_missing(self):
        # A hermeticity guard: the doctor check must never try to shell
        # out to a tool it already knows isn't there.
        config_path = self._write_projects_json({"projects": []})

        with mock.patch.object(server, "which", side_effect=_which_side_effect(set())), \
             mock.patch.object(server, "run_git") as run_git, \
             mock.patch.object(server, "run_backlog_raw") as run_backlog_raw:
            server.run_doctor_check(config_path=config_path)

        run_git.assert_not_called()
        run_backlog_raw.assert_not_called()

    # task-128: the staleness comparison, relayed from the running server

    def _check_with_running_server(self, board, port=7420):
        repo = self._make_project_dir("my-app")
        config_path = self._write_projects_json(
            {"port": port, "projects": [{"name": "my-app", "path": repo}]})
        with mock.patch.object(server, "which", side_effect=_which_side_effect({"git", "backlog", "tmux"})), \
             mock.patch.object(server, "run_git", return_value=_proc(0, "git version 2.43.0\n")), \
             mock.patch.object(server, "run_backlog_raw", return_value=_proc(0, "backlog/1.50.1\n")), \
             mock.patch.object(server, "probe_running_server", return_value=board) as probe:
            lines, ok = server.run_doctor_check(config_path=config_path)
        return lines, ok, probe

    def test_a_running_server_behind_its_checkout_is_a_warning_naming_both_commits(self):
        lines, ok, probe = self._check_with_running_server(
            {"version": "v0.1.0 (055ff59)",
             "codeDrift": {"loaded": "055ff59", "current": "a088d15", "commitsBehind": 23}},
            port=7431)
        probe.assert_called_once_with(7431)
        self.assertTrue(ok)  # advice, like a missing tmux: the checkout itself is fine
        self.assertEqual(
            lines[-1],
            "[WARN] the Centrale serving port 7431 started from 055ff59 but its checkout "
            "is now at a088d15, 23 commits later -- the code has changed since that "
            "process started; restart it to load it")

    def test_one_commit_and_an_uncountable_drift_read_naturally(self):
        lines, _, _ = self._check_with_running_server(
            {"version": "v", "codeDrift": {"loaded": "a", "current": "b", "commitsBehind": 1}})
        self.assertIn("now at b, 1 commit later --", lines[-1])
        lines, _, _ = self._check_with_running_server(
            {"version": "v", "codeDrift": {"loaded": "a", "current": "b", "commitsBehind": None}})
        self.assertIn("now at b -- the code has changed", lines[-1])

    def test_a_running_server_on_its_checkouts_head_passes(self):
        lines, ok, _ = self._check_with_running_server(
            {"version": "v0.1.0-14-g4570911", "codeDrift": None})
        self.assertTrue(ok)
        self.assertEqual(
            lines[-1],
            "[PASS] the Centrale serving port 7420 (v0.1.0-14-g4570911) is running its "
            "checkout's current code")

    def test_a_running_server_that_predates_the_check_is_told_to_restart(self):
        # An older server answers with a version but no comparison at
        # all -- which is itself the finding.
        lines, ok, _ = self._check_with_running_server({"version": "v0.1.0 (055ff59)"})
        self.assertTrue(ok)
        self.assertTrue(lines[-1].startswith("[WARN] the Centrale serving port 7420 (v0.1.0 (055ff59)) predates this check"))
        self.assertIn("restart it", lines[-1])

    def test_no_running_server_prints_nothing_about_one(self):
        lines, ok, probe = self._check_with_running_server(None)
        probe.assert_called_once_with(7420)
        self.assertTrue(ok)
        self.assertFalse(any("Centrale serving port" in line for line in lines))
        self.assertTrue(lines[-1].startswith("[PASS] my-app:"))

    def test_the_probe_is_never_reached_when_the_config_fails_to_load(self):
        path = os.path.join(self.tmpdir.name, "projects.json")
        with open(path, "w", encoding="utf-8") as f:
            f.write('{"agents": "not-an-object"}')  # normalize_agents_map raises ConfigError
        with mock.patch.object(server, "which", return_value=None), \
             mock.patch.object(server, "probe_running_server") as probe:
            _lines, ok = server.run_doctor_check(config_path=path)
        self.assertFalse(ok)
        probe.assert_not_called()

    def test_the_probe_answers_none_when_nothing_listens(self):
        # The real boundary against a closed loopback port: a refused
        # connection is "no server", not an exception out of --check.
        import socket
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            free_port = sock.getsockname()[1]
        self.assertIsNone(server.probe_running_server(free_port))


class DoctorCliWiringTests(unittest.TestCase):
    """`--check` must short-circuit main() before anything with a real
    side effect (signal handlers, binding a port, starting the harvest
    thread) runs -- it's meant to be safe to run anytime, including
    while a real Centrale instance is already up on the same port."""

    # main() prints the doctor's report on stdout, which is the whole
    # point of --check -- so every test here captures it (task-106).
    # Uncaptured, those [PASS]/[FAIL] lines escape into the test
    # runner's own output, and stdout buffering lands them *after*
    # unittest's "OK", making a green suite read like a broken one.

    def test_check_flag_exits_without_starting_the_server(self):
        stdout = io.StringIO()
        with mock.patch.object(server.sys, "argv", ["server.py", "--check"]), \
             mock.patch.object(server, "run_doctor_check", return_value=(["[PASS] ok"], True)) as doctor, \
             mock.patch.object(server, "install_terminate_handlers") as install_handlers, \
             mock.patch.object(server, "CentraleHTTPServer") as http_server:
            with contextlib.redirect_stdout(stdout), self.assertRaises(SystemExit) as ctx:
                server.main()

        doctor.assert_called_once_with()
        install_handlers.assert_not_called()
        http_server.assert_not_called()
        self.assertEqual(ctx.exception.code, 0)
        self.assertIn("[PASS] ok", stdout.getvalue())

    def test_check_flag_exit_code_reflects_failure(self):
        stdout = io.StringIO()
        with mock.patch.object(server.sys, "argv", ["server.py", "--check"]), \
             mock.patch.object(server, "run_doctor_check", return_value=(["[FAIL] nope"], False)):
            with contextlib.redirect_stdout(stdout), self.assertRaises(SystemExit) as ctx:
                server.main()

        self.assertEqual(ctx.exception.code, 1)
        self.assertIn("[FAIL] nope", stdout.getvalue())

    def test_no_check_flag_runs_the_server_as_normal(self):
        # install_terminate_handlers() itself is mocked out here (not just
        # signal.signal) so this never touches this test process's real
        # signal state -- same hermeticity requirement TerminateHandlerTests
        # in test_server.py already established.
        stderr = io.StringIO()
        with mock.patch.object(server.sys, "argv", ["server.py"]), \
             mock.patch.object(server, "run_doctor_check") as doctor, \
             mock.patch.object(server, "install_terminate_handlers"), \
             mock.patch.object(server, "load_config", side_effect=server.ConfigError("boom")):
            # load_config raising is the cheapest way to observe main()
            # took the normal startup path instead of --check's. main()
            # reports that failure on stderr, so capture it rather than
            # let a bare "Centrale: boom" surface in the suite's output.
            with contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit):
                server.main()

        doctor.assert_not_called()
        self.assertIn("Centrale: boom", stderr.getvalue())


class BacklogVersionParsingTests(unittest.TestCase):
    """task-156: reading a number out of somebody else's CLI output."""

    def test_it_takes_the_first_dotted_version_however_it_is_wrapped(self):
        # The bare number is what `backlog --version` prints today; the
        # rest are shapes a future release could print without anything
        # here being wrong. None of them is a contract, which is why the
        # read is a search rather than a parse.
        for output in ("1.51.0", "1.51.0\n", "backlog/1.51.0", "backlog v1.51.0 (node 20)"):
            with self.subTest(output=output):
                self.assertEqual(server.backlog_version(_proc(0, output)), "1.51.0")

    def test_it_reads_a_version_the_cli_wrote_to_stderr(self):
        self.assertEqual(server.backlog_version(_proc(0, "", "1.51.0\n")), "1.51.0")

    def test_output_with_no_version_in_it_is_none_not_a_guess(self):
        # "1.51.0.2" is deliberately here: it is a version, but not one
        # of the three-component shape being compared, and half of it is
        # a worse answer than none.
        for output in ("", "   ", "backlog", "1.51", "1.51.0.2"):
            with self.subTest(output=output):
                self.assertIsNone(server.backlog_version(_proc(0, output)))

    def test_a_failed_call_is_none_whatever_it_printed(self):
        self.assertIsNone(server.backlog_version(_proc(1, "1.51.0", "not found")))


class BacklogBaselineReportingTests(unittest.TestCase):
    """task-156: --check states the installed `backlog` against the
    version this release was tested on -- and never does more than state
    it. Centrale is a veneer over that CLI, so the gap is the first thing
    worth reading when the board looks wrong; it is also the ordinary
    state of a healthy machine within a fortnight of any release, since
    upstream ships a minor every few days. Hence: always a PASS."""

    def _backlog_line(self, output, baseline="1.51.0"):
        with mock.patch.object(server, "which", side_effect=_which_side_effect({"git", "backlog", "tmux"})), \
             mock.patch.object(server, "run_git", return_value=_proc(0, "git version 2.43.0\n")), \
             mock.patch.object(server.version, "TESTED_BACKLOG_VERSION", baseline), \
             mock.patch.object(server, "run_backlog_raw", return_value=output), \
             mock.patch.object(server, "load_config", return_value={"projects": []}):
            lines, ok = server.run_doctor_check(config_path="/nonexistent/projects.json")
        matching = [line for line in lines if "backlog" in line and "found on PATH" in line]
        self.assertEqual(len(matching), 1, lines)
        return matching[0], ok

    def test_the_matching_case_says_so_rather_than_saying_nothing(self):
        line, ok = self._backlog_line(_proc(0, "1.51.0\n"))
        self.assertTrue(ok)
        self.assertTrue(line.startswith("[PASS] backlog 1.51.0 found on PATH"), line)
        self.assertIn("tested against", line)

    def test_a_different_version_is_named_next_to_the_baseline_and_still_passes(self):
        # Both numbers in one line: the whole point is that a reader has
        # the comparison in front of them without going to the README.
        line, ok = self._backlog_line(_proc(0, "1.52.1\n"))
        self.assertTrue(ok)
        self.assertTrue(line.startswith("[PASS]"), line)
        self.assertIn("1.51.0", line)
        self.assertIn("1.52.1", line)
        self.assertIn("nothing to fix", line)

    def test_an_older_version_is_reported_the_same_way_and_never_refused(self):
        # Behind is not a lesser state than ahead here: Backlog.md
        # publishes no minimum, so Centrale has no floor to enforce and
        # inventing one would be a claim nobody measured.
        line, ok = self._backlog_line(_proc(0, "1.49.0\n"))
        self.assertTrue(ok)
        self.assertTrue(line.startswith("[PASS]"), line)
        self.assertIn("1.49.0", line)

    def test_a_cli_that_reports_no_version_still_names_the_baseline(self):
        # The comparison is what is lost, not the information: a reader
        # chasing a bug still learns which CLI this build was tested on.
        line, ok = self._backlog_line(_proc(1, "", "backlog: unknown flag"))
        self.assertTrue(ok)
        self.assertTrue(line.startswith("[PASS] backlog found on PATH"), line)
        self.assertIn("1.51.0", line)

    def test_no_version_gap_can_fail_the_check(self):
        # --check's exit status gates the release gate and is the first
        # thing a stranger runs. A dependency being newer than the
        # baseline must never be what stops either.
        for output in (_proc(0, "1.52.1\n"), _proc(0, "1.0.0\n"), _proc(1, "", "boom")):
            with self.subTest(output=output.stdout or output.stderr):
                line, ok = self._backlog_line(output)
                self.assertTrue(ok)
                self.assertNotIn("[FAIL]", line)
                self.assertNotIn("[WARN]", line)


if __name__ == "__main__":
    unittest.main()
