"""Hermetic tests for the parsing helpers in tests_integration/base.py,
and for the tier's own skip-or-fail switch.

The integration tier itself never runs here (it needs real `git`, `tmux`
and `backlog` processes -- see tests_integration/README.md), but
`base.parse_created_task_id` is a pure function over captured CLI text,
and importing `tests_integration.base` has no side effects beyond a few
`shutil.which` lookups. So it gets covered by the fast suite, where a
regression is caught in a second instead of surfacing as a confusing
'task not found' failure three steps into a real integration run.

The same goes for `base.require_tools` (task-130): what a missing tool
does to the tier -- a clean skip in development, a failure under the
release gate's CENTRALE_REQUIRE_INTEGRATION -- is a decision made before
any subprocess runs, so it is tested here with `which` patched, the way
tests/test_frontend_behaviour.py covers `js_harness.requires_node`.
"""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests_integration import base  # noqa: E402


def create_output(file_path, task_id, title, parent=None):
    """A faithful reproduction of what `backlog task create --plain`
    actually prints (captured from backlog 1.51.0, byte-identical to
    1.50.1's): the file path, then a full plain-text view of the new
    task."""
    parent_line = f"Parent: {parent}\n" if parent else ""
    return (
        f"File: {file_path}\n"
        "\n"
        f"Task {task_id} - {title}\n"
        "==================================================\n"
        "\n"
        "Status: ○ To Do\n"
        "Ordinal: 1000\n"
        "Created: 2026-09-03 10:43 (UTC)\n"
        f"{parent_line}"
        "\n"
        "Description:\n"
        "--------------------------------------------------\n"
        "No description provided\n"
    )


class ParseCreatedTaskIdTest(unittest.TestCase):
    def test_task_n_directory_in_temp_path_does_not_win(self):
        """The regression this helper exists for (task-88): a scratch repo
        under a per-task scratchpad puts `task-81` in the absolute path on
        the `File:` line, ahead of the real `task-1` filename. An
        unanchored search over the whole output returned TASK-81."""
        out = create_output(
            "/tmp/centrale-itest/centrale-task-81/abc-123/scratchpad/repo"
            "/backlog/tasks/task-1 - Finish-me.md",
            "TASK-1",
            "Finish me",
        )
        self.assertEqual(base.parse_created_task_id(out), "TASK-1")

    def test_dotted_subtask_id(self):
        out = create_output(
            "/tmp/centrale-task-81/repo/backlog/tasks/task-1.2 - Sub-probe.md",
            "TASK-1.2",
            "Sub probe",
            parent="TASK-1",
        )
        self.assertEqual(base.parse_created_task_id(out), "TASK-1.2")

    def test_plain_output_under_a_boring_temp_path(self):
        out = create_output(
            "/tmp/centrale-itest-9k2/repo/backlog/tasks/task-7 - Fix-the-login-bug.md",
            "TASK-7",
            "Fix the login bug",
        )
        self.assertEqual(base.parse_created_task_id(out), "TASK-7")

    def test_parent_line_does_not_win_over_the_heading(self):
        """`Parent: TASK-1` is printed *after* the heading, and mustn't be
        mistaken for the created task's own id."""
        out = create_output(
            "/tmp/repo/backlog/tasks/task-1.3 - Third-sub.md",
            "TASK-1.3",
            "Third sub",
            parent="TASK-1",
        )
        self.assertEqual(base.parse_created_task_id(out), "TASK-1.3")

    def test_title_mentioning_another_task_does_not_win(self):
        out = create_output(
            "/tmp/repo/backlog/tasks/task-4 - Revert-task-81-changes.md",
            "TASK-4",
            "Revert task-81 changes",
        )
        self.assertEqual(base.parse_created_task_id(out), "TASK-4")

    def test_falls_back_to_the_file_line_filename(self):
        """If the heading ever stops being printed, the `File:` line's
        `tasks/task-N` component is still authoritative -- and the *last*
        such component wins, so a `tasks/task-81` directory earlier in the
        path can't shadow the real filename."""
        out = (
            "File: /tmp/tasks/task-81/repo/backlog/tasks/task-2 - Finish-me.md\n"
        )
        self.assertEqual(base.parse_created_task_id(out), "TASK-2")

    def test_raises_with_the_output_when_nothing_matches(self):
        with self.assertRaises(AssertionError) as ctx:
            base.parse_created_task_id("backlog: unknown command\n")
        self.assertIn("backlog: unknown command", str(ctx.exception))

    def test_raises_on_empty_output(self):
        with self.assertRaises(AssertionError):
            base.parse_created_task_id("")


# ---------------------------------------------------------------------
# task-161: one tmux socket per test-run process
# ---------------------------------------------------------------------


class TmuxSocketNamespaceTest(unittest.TestCase):
    """Which socket the tier talks to is decided at import, before any
    subprocess runs -- so, like `require_tools` below, it is testable
    here. What this guards is a regression that would be invisible
    until two runs overlapped: re-pinning the name to a constant costs
    nothing on an idle machine and silently reintroduces one shared tmux
    server across concurrent runs and the release gate."""

    def test_a_fresh_name_is_unique_and_names_the_run_that_made_it(self):
        names = {base.new_tmux_socket_name() for _ in range(50)}
        self.assertEqual(len(names), 50, "socket names collided")
        for name in names:
            self.assertTrue(name.startswith(base.TMUX_SOCKET_PREFIX + "-"), name)
            # The pid is what makes a stray socket file traceable back to
            # the run that left it.
            self.assertIn(str(os.getpid()), name)

    def test_the_module_picked_one_rather_than_pinning_a_constant(self):
        self.assertTrue(base.TMUX_SOCKET.startswith(base.TMUX_SOCKET_PREFIX + "-"),
                        base.TMUX_SOCKET)
        self.assertNotEqual(base.TMUX_SOCKET, base.TMUX_SOCKET_PREFIX)

    def test_the_socket_path_is_where_tmux_actually_puts_it(self):
        uid_dir = "tmux-%d" % os.getuid()
        with mock.patch.dict(os.environ, {"TMUX_TMPDIR": "/run/user/9/tmuxtmp"}):
            self.assertEqual(base.tmux_socket_path("sock"),
                             os.path.join("/run/user/9/tmuxtmp", uid_dir, "sock"))
        # tmux falls back to the literal /tmp, and does NOT read TMPDIR
        # -- which this tier redirects for the release tests, so reading
        # it here would point teardown at a path no socket is at.
        with mock.patch.dict(os.environ, {"TMUX_TMPDIR": "", "TMPDIR": "/somewhere/else"}):
            self.assertEqual(base.tmux_socket_path("sock"),
                             os.path.join("/tmp", uid_dir, "sock"))
        with mock.patch.dict(os.environ, {}):
            os.environ.pop("TMUX_TMPDIR", None)
            self.assertEqual(base.tmux_socket_path(),
                             os.path.join("/tmp", uid_dir, base.TMUX_SOCKET))

    def test_the_name_leaves_room_inside_a_unix_socket_path(self):
        """A unix socket path is capped at ~108 bytes and tmux fails
        outright past it (a lesson from task-72's probe, which had to
        move a socket out of a long scratchpad path)."""
        self.assertLess(len(base.tmux_socket_path().encode("utf-8")), 100)


# ---------------------------------------------------------------------
# task-130: the tier may not skip its way past a release
# ---------------------------------------------------------------------


class RequireToolsSwitchTest(unittest.TestCase):
    """`require_tools` skips a test whose tool is missing -- unless
    CENTRALE_REQUIRE_INTEGRATION is set, when the same absence is a
    failure naming the tool. scripts/release.sh's gate is the only
    thing that sets it; on a development machine nothing changes."""

    def _result_of(self, case):
        result = unittest.TestResult()
        unittest.defaultTestLoader.loadTestsFromTestCase(case).run(result)
        return result

    def _probe(self, decorate_class, tools=("git", "definitely-missing-tool")):
        """A one-test TestCase behind `require_tools`, decorated as a
        class or as a method (both spellings are in use across the
        tier), recording whether its setUp and body ever ran."""
        ran = []

        class Probe(unittest.TestCase):
            def setUp(self):
                ran.append("setUp")

            def test_body(self):
                ran.append("body")

        if decorate_class:
            Probe = base.require_tools(*tools)(Probe)
        else:
            Probe.test_body = base.require_tools(*tools)(Probe.test_body)
        return Probe, ran

    def _without(self, missing, *, required):
        """Context: `missing` absent from PATH (every other tool present),
        and the switch set or unset."""
        def which(name):
            return None if name == missing else "/usr/bin/" + name
        env = {base.REQUIRE_INTEGRATION_ENV: "1"} if required else {}
        return mock.patch.object(base, "which", side_effect=which), \
            mock.patch.dict(os.environ, env)

    def test_a_missing_tool_is_a_clean_skip_in_ordinary_development(self):
        which, env = self._without("definitely-missing-tool", required=False)
        with which, env:
            os.environ.pop(base.REQUIRE_INTEGRATION_ENV, None)
            for decorate_class in (False, True):
                with self.subTest(decorate_class=decorate_class):
                    probe, ran = self._probe(decorate_class)
                    result = self._result_of(probe)

                    self.assertEqual(len(result.skipped), 1, result.skipped)
                    self.assertIn("definitely-missing-tool", result.skipped[0][1])
                    self.assertEqual(result.failures, [])
                    self.assertEqual(result.errors, [])
                    self.assertEqual(ran, [], "nothing must have run")

    def test_the_switch_turns_that_skip_into_a_failure(self):
        which, env = self._without("definitely-missing-tool", required=True)
        with which, env:
            for decorate_class in (False, True):
                with self.subTest(decorate_class=decorate_class):
                    probe, ran = self._probe(decorate_class)
                    result = self._result_of(probe)

                    # A FAILURE, not a skip and not an error: the tier
                    # could not run, and a release must hear about it.
                    self.assertEqual(result.skipped, [])
                    self.assertEqual(result.errors, [])
                    self.assertEqual(len(result.failures), 1, result.failures)
                    message = result.failures[0][1]
                    self.assertIn("definitely-missing-tool", message)
                    self.assertIn(base.REQUIRE_INTEGRATION_ENV, message)
                    self.assertIn("may not skip", message)
                    # A method-level gate cannot stop the class's setUp
                    # (the next test covers why the class form does).
                    self.assertNotIn("body", ran, "the body must not have run")

    def test_a_class_failure_does_not_run_the_fixture_first(self):
        """The fixture is usually where the missing tool is first shelled
        out to: a `git init` in setUp would turn the verdict into an
        unrelated-looking error before the test method was reached."""
        which, env = self._without("git", required=True)
        with which, env:
            probe, ran = self._probe(decorate_class=True)
            result = self._result_of(probe)

            self.assertEqual(result.errors, [])
            self.assertEqual(len(result.failures), 1, result.failures)
            self.assertIn("git", result.failures[0][1])
            self.assertEqual(ran, [])

    def test_a_machine_with_every_tool_is_untouched_either_way(self):
        for required in (False, True):
            with self.subTest(required=required):
                which, env = self._without("nothing-is-missing", required=required)
                with which, env:
                    if not required:
                        os.environ.pop(base.REQUIRE_INTEGRATION_ENV, None)
                    probe, ran = self._probe(decorate_class=False, tools=("git", "tmux"))
                    result = self._result_of(probe)

                    self.assertEqual(result.skipped, [])
                    self.assertEqual(result.failures, [])
                    self.assertEqual(result.errors, [])
                    self.assertEqual(ran, ["setUp", "body"])

    def test_only_a_meaningful_value_arms_the_switch(self):
        for value, expected in (("1", True), ("yes", True), (" ", False),
                                ("", False), ("0", False)):
            with self.subTest(value=value):
                with mock.patch.dict(os.environ, {base.REQUIRE_INTEGRATION_ENV: value}):
                    self.assertEqual(base.integration_required(), expected)
        with mock.patch.dict(os.environ, {}):
            os.environ.pop(base.REQUIRE_INTEGRATION_ENV, None)
            self.assertFalse(base.integration_required())

    def test_only_the_release_gate_arms_the_switch(self):
        # The switch is a release's, and only a release's: a stray export
        # anywhere in the executable tree would make every developer's
        # integration run fail on a machine without tmux or backlog,
        # which is the clean skip the tier's README promises. (Prose that
        # names the variable is not a setter, so the docs are not read.)
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        setters = []
        for where in (root, os.path.join(root, "scripts"),
                      os.path.join(root, "tests"),
                      os.path.join(root, "tests_integration")):
            for name in sorted(os.listdir(where)):
                path = os.path.join(where, name)
                if not os.path.isfile(path) or not name.endswith((".py", ".sh")):
                    continue
                with open(path, "r", encoding="utf-8", errors="replace") as f:
                    for lineno, line in enumerate(f, 1):
                        if base.REQUIRE_INTEGRATION_ENV + "=" in line:
                            setters.append("%s:%d" % (path[len(root) + 1:], lineno))
        self.assertTrue(
            any(s.startswith("scripts/release.sh:") for s in setters),
            "the release gate must be the thing that sets it: " + repr(setters))
        stray = [s for s in setters
                 if not s.startswith(("scripts/release.sh:",
                                      "tests/test_integration_base.py:",
                                      "tests_integration/test_release_integration.py:"))]
        self.assertEqual(stray, [], "unexpected setter(s) of " + base.REQUIRE_INTEGRATION_ENV)


if __name__ == "__main__":
    unittest.main()
