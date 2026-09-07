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

import server  # noqa: E402
import spawn  # noqa: E402

FIXTURES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def make_config(worktree_root, projects):
    return {
        "port": 0,
        "worktreeRoot": worktree_root,
        "projects": projects,
    }


def git_proc(args, returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(["git", *args], returncode, stdout, stderr)


def tmux_proc(args, returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(["tmux", *args], returncode, stdout, stderr)


def backlog_raw_proc(args=(), returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(["backlog", *args], returncode, stdout, stderr)


def new_session_argv(run_tmux):
    """The argv of the one `tmux new-session` call a patched `run_tmux`
    saw, asserting there was exactly one.

    Since task-113 launching a session is two tmux calls -- new-session,
    then the `set-option ... window-size manual` that pins its geometry
    -- so `run_tmux.call_args` (the LAST call) is no longer the
    interesting one, and `assert_called_once()` no longer says what it
    used to. This helper carries both halves of the old assertion: the
    argv to inspect, and "exactly one session was launched".
    """
    launches = [c.args[0] for c in run_tmux.call_args_list if c.args[0][0] == "new-session"]
    if len(launches) != 1:
        raise AssertionError(f"expected exactly one new-session call, got {launches!r}")
    return launches[0]


def geometry_args():
    """The `-x W -y H` pair spawn/resume put in every new-session argv,
    derived from spawn.SESSION_GEOMETRY so the constant stays the single
    source of truth (task-113)."""
    columns, rows = spawn.SESSION_GEOMETRY
    return ["-x", str(columns), "-y", str(rows)]


class FakeGit:
    """Records every `git` invocation and answers the small set of git
    subcommands spawn.py needs, with configurable existence state."""

    def __init__(self, branch_exists=False, worktree_registered=False, wt_dir=None,
                 current_branch="main"):
        self.calls = []
        self.branch_exists = branch_exists
        self.worktree_registered = worktree_registered
        self.wt_dir = wt_dir
        self.current_branch = current_branch

    def __call__(self, args, cwd=None):
        self.calls.append(list(args))
        if args[:1] == ["symbolic-ref"]:
            return git_proc(args, 0, self.current_branch + "\n", "")
        if args[:1] == ["rev-parse"] and "--abbrev-ref" in args:
            return git_proc(args, 0, self.current_branch + "\n", "")
        if args[:1] == ["rev-parse"] and "--verify" in args:
            return git_proc(args, 0 if self.branch_exists else 1, "", "")
        if args[:1] == ["worktree"] and args[1:2] == ["list"]:
            stdout = ""
            if self.worktree_registered and self.wt_dir:
                stdout = f"worktree {self.wt_dir}\nHEAD abc123\nbranch refs/heads/x\n\n"
            return git_proc(args, 0, stdout, "")
        if args[:1] == ["worktree"] and args[1:2] == ["add"]:
            return git_proc(args, 0, "", "")
        return git_proc(args, 0, "", "")


class SpawnHelperTests(unittest.TestCase):
    def test_session_name(self):
        self.assertEqual(spawn.session_name("my-app", "TASK-2"), "centrale-my-app-task-2")

    def test_session_name_encodes_dotted_subtask_id(self):
        # task-59: tmux forbids "." in a session name (it's the
        # window/pane-index separator in a -t target) and silently
        # rewrites it to "_" -- session_name() must encode up front
        # rather than ever handing tmux a name it would mangle.
        self.assertEqual(spawn.session_name("my-lib", "TASK-11.2"), "centrale-my-lib-task-11_2")

    def test_worktree_dir(self):
        config = {"worktreeRoot": "/root"}
        self.assertEqual(
            spawn.worktree_dir(config, "my-app", "TASK-2"), "/root/my-app-task-2"
        )

    def test_worktree_dir_keeps_dot_for_dotted_subtask_id(self):
        # Unlike session_name, worktree_dir must NOT encode the dot --
        # filesystem paths and git refs allow "." natively, and an
        # existing worktree/branch for a subtask must keep resolving to
        # the same directory after task-59.
        config = {"worktreeRoot": "/root"}
        self.assertEqual(
            spawn.worktree_dir(config, "my-lib", "TASK-11.2"), "/root/my-lib-task-11.2"
        )

    def test_branch_name(self):
        self.assertEqual(spawn.branch_name("TASK-2"), "task/task-2")

    def test_branch_name_keeps_dot_for_dotted_subtask_id(self):
        self.assertEqual(spawn.branch_name("TASK-11.2"), "task/task-11.2")

    def test_encode_task_id_for_session_replaces_every_dot(self):
        self.assertEqual(spawn.encode_task_id_for_session("TASK-11.2"), "task-11_2")
        self.assertEqual(spawn.encode_task_id_for_session("TASK-2"), "task-2")

    def test_decode_session_task_id_is_inverse_of_encode(self):
        for task_id in ("TASK-2", "TASK-11.2", "TASK-11.2.3"):
            encoded = spawn.encode_task_id_for_session(task_id)
            self.assertEqual(spawn.decode_session_task_id(encoded), task_id.lower())

    def test_is_encoded_task_id_accepts_underscored_shape(self):
        self.assertTrue(spawn.is_encoded_task_id("task-11_2"))
        self.assertTrue(spawn.is_encoded_task_id("task-2"))
        self.assertTrue(spawn.is_encoded_task_id("task-11_2_3"))

    def test_is_encoded_task_id_rejects_garbage(self):
        self.assertFalse(spawn.is_encoded_task_id("task-11.2"))  # a literal dot, not encoded
        self.assertFalse(spawn.is_encoded_task_id("not_a_task_id"))
        self.assertFalse(spawn.is_encoded_task_id(""))

    def test_prompt_matches_design_exactly(self):
        expected = (
            "Work on backlog task TASK-2. Follow the Backlog.md workflow: run "
            "`backlog instructions overview` first, claim the task, implement it, "
            "check off acceptance criteria as you meet them, add implementation "
            "notes, and update the status when done. Before designing anything, "
            "check the repo's standing decision records (`backlog decision list "
            "--plain`) and treat them as constraints. When you are done, commit "
            "all your work on this branch, including the backlog task updates. "
            "Do NOT merge this branch into the default branch or delete it -- "
            "the dashboard's gated merge handles integration."
        )
        self.assertEqual(spawn.prompt_for("TASK-2"), expected)

    def test_prompt_consults_standing_decisions_before_designing(self):
        # Task-69: decision records are the durable "why" layer; the
        # default prompt points every agent at them with exactly one
        # command (free on a repo with none: "No decisions found.").
        prompt = spawn.prompt_for("TASK-2")
        self.assertIn("`backlog decision list --plain`", prompt)
        self.assertIn("treat them as constraints", prompt)
        self.assertEqual(prompt.count("backlog decision"), 1)

    def test_prompt_for_without_config_or_without_override_uses_the_default(self):
        self.assertEqual(spawn.prompt_for("TASK-2", None), spawn.PROMPT_TEMPLATE.format(task_id="TASK-2"))
        self.assertEqual(spawn.prompt_for("TASK-2", {}), spawn.PROMPT_TEMPLATE.format(task_id="TASK-2"))
        self.assertEqual(
            spawn.prompt_for("TASK-2", {"spawnPrompt": None}),
            spawn.PROMPT_TEMPLATE.format(task_id="TASK-2"),
        )

    def test_prompt_for_uses_a_configured_spawn_prompt_verbatim(self):
        # Task-69: a top-level projects.json "spawnPrompt" replaces the
        # built-in template outright -- nothing from the default is
        # smuggled back in (not even the no-self-merge rule: the user owns
        # the whole template once they override it).
        config = {"spawnPrompt": "Pick up {task_id}; keep it small.\n\nTrailing whitespace kept.  "}
        self.assertEqual(
            spawn.prompt_for("TASK-2", config),
            "Pick up TASK-2; keep it small.\n\nTrailing whitespace kept.  ",
        )
        self.assertNotIn("Do NOT merge", spawn.prompt_for("TASK-2", config))

    def test_prompt_carries_the_no_self_merge_boundary(self):
        # Task-36: a coordination rule discovered via a live double-merge
        # (an agent merged its own branch, bypassing the gates) -- this
        # must ship in the product's prompt, not per-user repo config.
        prompt = spawn.prompt_for("TASK-2")
        self.assertIn("Do NOT merge this branch", prompt)
        self.assertIn("gated merge handles integration", prompt)

    def test_spawn_cmd_defaults_to_claude(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CENTRALE_SPAWN_CMD", None)
            self.assertEqual(spawn.spawn_cmd(), ["claude"])

    def test_spawn_cmd_parses_env_override_with_shlex(self):
        with mock.patch.dict(os.environ, {"CENTRALE_SPAWN_CMD": "sleep 300"}):
            self.assertEqual(spawn.spawn_cmd(), ["sleep", "300"])

    def test_spawn_cmd_handles_quoted_args(self):
        with mock.patch.dict(os.environ, {"CENTRALE_SPAWN_CMD": "echo 'hello world'"}):
            self.assertEqual(spawn.spawn_cmd(), ["echo", "hello world"])


class AgentKindForCommandTests(unittest.TestCase):
    def test_recognizes_built_in_command_basenames(self):
        self.assertEqual(spawn.agent_kind_for_command(["claude"]), "claude")
        self.assertEqual(spawn.agent_kind_for_command(["/opt/codex/bin/codex", "exec"]), "codex")

    def test_claude_family_name_depends_on_command_not_config_key(self):
        self.assertEqual(
            spawn.agent_kind_for_command(["claude", "--model", "sonnet"]),
            "claude",
        )

    def test_custom_or_empty_command_has_no_builtin_kind(self):
        self.assertIsNone(spawn.agent_kind_for_command(["my-wrapper"]))
        self.assertIsNone(spawn.agent_kind_for_command([]))


class EventUrlTests(unittest.TestCase):
    """Unit tests for spawn.event_url -- the CENTRALE_EVENT_URL every
    spawned/resumed session's environment carries (task-37 AC #4)."""

    def test_default_port_when_config_omits_port(self):
        self.assertEqual(
            spawn.event_url({}, "my-app", "TASK-2"),
            "http://127.0.0.1:7420/api/agent-event?project=my-app&task=TASK-2",
        )

    def test_uses_the_configured_port(self):
        self.assertEqual(
            spawn.event_url({"port": 9001}, "my-app", "TASK-2"),
            "http://127.0.0.1:9001/api/agent-event?project=my-app&task=TASK-2",
        )

    def test_project_and_task_are_query_encoded(self):
        url = spawn.event_url({"port": 7420}, "my project", "TASK-2")
        self.assertIn("project=my+project", url)


    def test_includes_validated_builtin_agent_kind(self):
        self.assertEqual(
            spawn.event_url({}, "my-app", "TASK-2", agent_kind="codex"),
            "http://127.0.0.1:7420/api/agent-event?project=my-app&task=TASK-2&agentKind=codex",
        )


class InjectAgentHooksTests(unittest.TestCase):
    """Unit tests for spawn._inject_agent_hooks -- the extra argv
    appended to a resolved agent's cmd (before any prompt argument) so it
    reports its own lifecycle back to CENTRALE_EVENT_URL (task-37 AC #2/#3,
    task-44 AC #1/#3). Shared verbatim by spawn() and resume() (AC #6);
    their own integration coverage lives in
    SpawnAgentSelectionIntegrationTests / ResumeIntegrationTests.

    task-44 dropped the `wt_dir` argument entirely: codex used to get a
    hooks.json file written into the worktree (task-42), but a linked
    worktree's `.git` FILE means codex resolves its project config layer
    through to the MAIN repo root, so that file was never actually
    discovered in the environment centrale spawns into. The fix passes
    the same four hook definitions as inline -c config overrides on the
    argv itself instead (server.codex_hooks_overrides -- pure, no
    filesystem or subprocess calls), so _inject_agent_hooks needs
    nothing worktree-specific any more. The codex cases here still mock
    server.probe_codex_hook_trust -- never the real thing, since this
    machine may have a real codex binary on PATH and a test must never
    shell out to it."""

    def test_empty_cmd_returns_nothing(self):
        self.assertEqual(spawn._inject_agent_hooks([]), [])

    def test_unrecognized_agent_binary_gets_no_injection(self):
        # CENTRALE_EVENT_URL alone (always set, see EventUrlTests) is the
        # whole contract for an agent Centrale doesn't specifically know
        # how to hook -- e.g. a fully custom wrapper script.
        self.assertEqual(spawn._inject_agent_hooks(["my-wrapper-script"]), [])

    def test_claude_gets_settings_flag_pointing_at_the_generated_file(self):
        with mock.patch.object(server, "ensure_hooks_settings_file", return_value="/cache/hooks-settings.json"):
            extra = spawn._inject_agent_hooks(["claude"])
        self.assertEqual(extra, ["--settings", "/cache/hooks-settings.json"])

    def test_claude_family_binary_with_a_different_agent_name_still_matches(self):
        # Dispatch is on os.path.basename(cmd[0]), not the config's agent
        # name -- "claude-sonnet": ["claude", "--model", "sonnet"] still
        # counts, same basename check resume() uses for "claude family".
        with mock.patch.object(server, "ensure_hooks_settings_file", return_value="/cache/hooks-settings.json"):
            extra = spawn._inject_agent_hooks(["claude", "--model", "sonnet"])
        self.assertEqual(extra, ["--settings", "/cache/hooks-settings.json"])

    def test_claude_settings_generation_failure_degrades_to_no_injection(self):
        # Best-effort: a read-only cache dir must never block the spawn
        # itself, just its agentState reporting.
        with mock.patch.object(server, "ensure_hooks_settings_file", side_effect=OSError("read-only")):
            extra = spawn._inject_agent_hooks(["claude"])
        self.assertEqual(extra, [])

    def test_codex_gets_notify_override_reporting_finished(self):
        with mock.patch.object(server, "probe_codex_hook_trust", return_value=True):
            extra = spawn._inject_agent_hooks(["codex"])
        self.assertEqual(extra[0], "-c")
        self.assertTrue(extra[1].startswith("notify="))
        notify_argv = json.loads(extra[1][len("notify="):])
        self.assertEqual(notify_argv, ["python3", server.notify_script_path(), "finished"])

    def test_codex_with_a_subcommand_still_matches_on_basename(self):
        with mock.patch.object(server, "probe_codex_hook_trust", return_value=True):
            extra = spawn._inject_agent_hooks(["codex", "exec"])
        self.assertEqual(extra[0], "-c")
        self.assertIn("finished", extra[1])

    def test_codex_probes_using_cmd0_as_the_binary_path(self):
        # The probe target is cmd[0] itself (e.g. a configured absolute
        # path to an alternate codex binary), not a hardcoded "codex".
        with mock.patch.object(server, "probe_codex_hook_trust", return_value=False) as probe:
            spawn._inject_agent_hooks(["/opt/codex-nightly/codex", "exec"])
        probe.assert_called_once_with("/opt/codex-nightly/codex")

    def test_codex_gets_the_four_hook_overrides_and_trust_flag_when_probe_is_positive(self):
        # task-44 AC #1: the four -c hooks.<Point>=... overrides (the
        # exact values server.codex_hooks_overrides() builds) follow the
        # notify override, and --dangerously-bypass-hook-trust follows
        # those, only once server.probe_codex_hook_trust(cmd[0]) says
        # this binary supports it.
        with mock.patch.object(server, "probe_codex_hook_trust", return_value=True):
            extra = spawn._inject_agent_hooks(["codex"])
            overrides = server.codex_hooks_overrides()
        self.assertEqual(len(extra), 2 + len(overrides) + 1)
        self.assertEqual(extra[2:2 + len(overrides)], overrides)
        self.assertEqual(extra[-1], "--dangerously-bypass-hook-trust")

    def test_codex_degrades_to_notify_only_when_probe_is_negative(self):
        # An older codex (or one without the hooks engine) never gets the
        # hook overrides or the trust flag -- only the notify override it
        # can safely digest.
        with mock.patch.object(server, "probe_codex_hook_trust", return_value=False):
            extra = spawn._inject_agent_hooks(["codex"])
        self.assertEqual(len(extra), 2)
        self.assertEqual(extra, ["-c", extra[1]])
        self.assertNotIn("--dangerously-bypass-hook-trust", extra)

    def test_codex_degrades_to_notify_only_when_probe_raises(self):
        # A probe failure (exception, not just a negative result -- e.g.
        # server.probe_codex_hook_trust's own subprocess call blowing up
        # in some unexpected way) is treated exactly like a negative
        # probe: notify-only, never a crash, never the trust flag.
        with mock.patch.object(server, "probe_codex_hook_trust", side_effect=RuntimeError("boom")):
            extra = spawn._inject_agent_hooks(["codex"])
        self.assertEqual(len(extra), 2)
        self.assertNotIn("--dangerously-bypass-hook-trust", extra)

    def test_codex_writes_no_hooks_file_and_makes_no_git_call(self):
        # task-44 AC #2: no hooks-related file is ever written into the
        # worktree and no git call (e.g. the old info/exclude write) is
        # ever made for a codex spawn -- the overrides live entirely on
        # the argv, so this path touches neither the filesystem nor git,
        # positive probe or not.
        with mock.patch.object(server, "probe_codex_hook_trust", return_value=True), \
             mock.patch.object(server, "run_git") as run_git, \
             mock.patch("builtins.open") as fake_open:
            spawn._inject_agent_hooks(["codex"])
        run_git.assert_not_called()
        fake_open.assert_not_called()

    def test_claude_and_custom_agents_are_byte_identical_to_before(self):
        # task-44 AC #3: only the codex path changed -- claude and a
        # custom/unrecognized agent must still behave exactly as before.
        with mock.patch.object(server, "ensure_hooks_settings_file", return_value="/cache/hooks-settings.json"):
            self.assertEqual(spawn._inject_agent_hooks(["claude"]), ["--settings", "/cache/hooks-settings.json"])
        self.assertEqual(spawn._inject_agent_hooks(["my-wrapper-script"]), [])
        self.assertEqual(spawn._inject_agent_hooks([]), [])


class WorktreeRootResolutionTests(unittest.TestCase):
    """resolve_worktree_root/worktree_dir are THE one shared place every
    worktree path is derived (spawn, resume, harvest, board, sessions) --
    these tests cover its '@repo' resolution directly, in isolation from
    the rest of spawn(). A plain path passing through unchanged is
    already covered by SpawnHelperTests.test_worktree_dir."""

    def test_global_at_repo_resolves_to_project_path_centrale_worktrees(self):
        config = make_config("@repo", [{"name": "my-app", "path": "/repos/my-app"}])
        self.assertEqual(
            spawn.resolve_worktree_root(config, "my-app"),
            "/repos/my-app/.centrale-worktrees",
        )

    def test_worktree_dir_end_to_end_with_at_repo(self):
        config = make_config("@repo", [{"name": "my-app", "path": "/repos/my-app"}])
        self.assertEqual(
            spawn.worktree_dir(config, "my-app", "TASK-2"),
            "/repos/my-app/.centrale-worktrees/my-app-task-2",
        )

    def test_per_project_override_at_repo_wins_even_when_global_is_plain(self):
        config = make_config(
            "/shared/worktrees",
            [{"name": "my-app", "path": "/repos/my-app", "worktreeRoot": "@repo"}],
        )
        self.assertEqual(
            spawn.resolve_worktree_root(config, "my-app"),
            "/repos/my-app/.centrale-worktrees",
        )

    def test_per_project_plain_override_wins_even_when_global_is_at_repo(self):
        config = make_config(
            "@repo",
            [{"name": "my-app", "path": "/repos/my-app", "worktreeRoot": "/custom/my-app-worktrees"}],
        )
        self.assertEqual(spawn.resolve_worktree_root(config, "my-app"), "/custom/my-app-worktrees")

    def test_two_projects_with_at_repo_each_get_their_own_path(self):
        config = make_config(
            "@repo",
            [
                {"name": "my-app", "path": "/repos/my-app"},
                {"name": "my-lib", "path": "/repos/my-lib"},
            ],
        )
        self.assertEqual(spawn.resolve_worktree_root(config, "my-app"), "/repos/my-app/.centrale-worktrees")
        self.assertEqual(spawn.resolve_worktree_root(config, "my-lib"), "/repos/my-lib/.centrale-worktrees")

    def test_unknown_project_with_at_repo_raises(self):
        config = make_config("@repo", [{"name": "my-app", "path": "/repos/my-app"}])
        with self.assertRaises(ValueError):
            spawn.resolve_worktree_root(config, "ghost")

    def test_unknown_project_with_plain_root_does_not_raise(self):
        # "@repo" is the only case that needs the project to resolve --
        # a plain root never depended on the project existing at all.
        config = make_config("/shared/worktrees", [])
        self.assertEqual(spawn.resolve_worktree_root(config, "ghost"), "/shared/worktrees")


class RepoRelativeWorktreeRootTests(unittest.TestCase):
    def test_inside_repo_returns_relative_path(self):
        self.assertEqual(
            spawn._repo_relative_worktree_root("/repos/my-app", "/repos/my-app/.centrale-worktrees"),
            ".centrale-worktrees",
        )

    def test_nested_inside_repo_returns_full_relative_path(self):
        self.assertEqual(
            spawn._repo_relative_worktree_root("/repos/my-app", "/repos/my-app/a/b"),
            "a/b",
        )

    def test_equal_to_repo_returns_none(self):
        self.assertIsNone(spawn._repo_relative_worktree_root("/repos/my-app", "/repos/my-app"))

    def test_sibling_directory_returns_none(self):
        self.assertIsNone(spawn._repo_relative_worktree_root("/repos/my-app", "/repos/.centrale-worktrees"))

    def test_unrelated_directory_returns_none(self):
        self.assertIsNone(spawn._repo_relative_worktree_root("/repos/my-app", "/tmp/somewhere-else"))

    def test_lookalike_prefix_is_not_treated_as_inside(self):
        # "/repos/my-app-other" starts with the string "/repos/my-app" but
        # is NOT actually inside it -- a naive string-prefix check would
        # get this wrong; realpath + os.sep-based comparison must not.
        self.assertIsNone(spawn._repo_relative_worktree_root("/repos/my-app", "/repos/my-app-other"))


class GitExcludeIdempotenceTests(unittest.TestCase):
    """_ensure_worktree_root_excluded does real file I/O (unlike every
    other spawn.py helper, which only ever touches git/tmux through the
    injectable server.run_git/run_tmux boundaries) -- so these use a real
    temp directory standing in for a repo, cleaned up via addCleanup,
    rather than mocking the filesystem."""

    def setUp(self):
        import tempfile
        self.repo = tempfile.mkdtemp(prefix="centrale-test-repo-")
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        import shutil
        shutil.rmtree(self.repo, ignore_errors=True)

    def _exclude_path(self):
        return os.path.join(self.repo, ".git", "info", "exclude")

    def _exclude_contents(self):
        with open(self._exclude_path(), "r", encoding="utf-8") as f:
            return f.read()

    def test_creates_exclude_file_and_adds_entry(self):
        worktree_root = os.path.join(self.repo, ".centrale-worktrees")
        spawn._ensure_worktree_root_excluded(self.repo, worktree_root)
        self.assertIn(".centrale-worktrees/", self._exclude_contents().splitlines())

    def test_second_call_does_not_duplicate_the_entry(self):
        worktree_root = os.path.join(self.repo, ".centrale-worktrees")
        spawn._ensure_worktree_root_excluded(self.repo, worktree_root)
        spawn._ensure_worktree_root_excluded(self.repo, worktree_root)
        lines = self._exclude_contents().splitlines()
        self.assertEqual(lines.count(".centrale-worktrees/"), 1)

    def test_preserves_existing_exclude_content(self):
        os.makedirs(os.path.dirname(self._exclude_path()), exist_ok=True)
        with open(self._exclude_path(), "w", encoding="utf-8") as f:
            f.write("*.pyc\n")
        worktree_root = os.path.join(self.repo, ".centrale-worktrees")
        spawn._ensure_worktree_root_excluded(self.repo, worktree_root)
        lines = self._exclude_contents().splitlines()
        self.assertIn("*.pyc", lines)
        self.assertIn(".centrale-worktrees/", lines)

    def test_noop_when_worktree_root_is_outside_the_repo(self):
        sibling_root = os.path.join(os.path.dirname(self.repo), "elsewhere-worktrees")
        spawn._ensure_worktree_root_excluded(self.repo, sibling_root)
        self.assertFalse(os.path.exists(self._exclude_path()))

    def test_best_effort_swallows_write_failures(self):
        worktree_root = os.path.join(self.repo, ".centrale-worktrees")
        with mock.patch("builtins.open", side_effect=OSError("read-only filesystem")):
            spawn._ensure_worktree_root_excluded(self.repo, worktree_root)  # must not raise


def task_view(assignees, status=None):
    task = {"id": "TASK-2", "assignees": assignees}
    if status is not None:
        task["status"] = status
    return {
        "schemaVersion": 1,
        "kind": "task-view",
        "task": task,
    }


class AgentResolutionTests(unittest.TestCase):
    """Unit tests for spawn.resolve_agent, in isolation from the rest of
    spawn(). resolve_agent trusts config['agents'] is already normalized
    (server.normalize_agents_map / server.load_config's job) — so every
    config built here runs its 'agents' dict through
    server.normalize_agents_map first, exactly as load_config would,
    before handing it to resolve_agent. Object-form vs. plain-list-form
    parsing itself is covered in test_server.py; this class is purely
    about resolve_agent's assignee-matching and fallback behavior."""

    def setUp(self):
        self.project = {"name": "my-app", "path": "/repos/my-app"}

    def _config(self, raw_agents, default_agent="claude"):
        return {"agents": server.normalize_agents_map(raw_agents), "defaultAgent": default_agent}

    def test_codex_assignee_resolves_codex_argv(self):
        config = self._config({"claude": ["claude"], "codex": ["codex"]})
        with mock.patch.object(server, "run_backlog", return_value=task_view(["@codex"])):
            agent_name, argv, suffix = spawn.resolve_agent(config, self.project, "TASK-2")
        self.assertEqual(agent_name, "codex")
        self.assertEqual(argv, ["codex"])
        self.assertIsNone(suffix)

    def test_claude_assignee_resolves_claude_argv(self):
        config = self._config({"claude": ["claude"], "codex": ["codex"]})
        with mock.patch.object(server, "run_backlog", return_value=task_view(["@claude"])):
            agent_name, argv, suffix = spawn.resolve_agent(config, self.project, "TASK-2")
        self.assertEqual(agent_name, "claude")
        self.assertEqual(argv, ["claude"])
        self.assertIsNone(suffix)

    def test_unassigned_falls_back_to_default_agent(self):
        config = self._config({"claude": ["claude"], "codex": ["codex"]})
        with mock.patch.object(server, "run_backlog", return_value=task_view([])):
            agent_name, argv, suffix = spawn.resolve_agent(config, self.project, "TASK-2")
        self.assertEqual(agent_name, "claude")
        self.assertEqual(argv, ["claude"])
        self.assertIsNone(suffix)

    def test_unmatched_assignee_falls_back_to_default_agent(self):
        config = self._config({"claude": ["claude"], "codex": ["codex"]}, default_agent="codex")
        with mock.patch.object(server, "run_backlog", return_value=task_view(["@someone-else"])):
            agent_name, argv, suffix = spawn.resolve_agent(config, self.project, "TASK-2")
        self.assertEqual(agent_name, "codex")
        self.assertEqual(argv, ["codex"])
        self.assertIsNone(suffix)

    def test_assignee_match_is_case_insensitive_and_strips_at_sign(self):
        config = self._config({"claude": ["claude"], "codex": ["codex"]})
        with mock.patch.object(server, "run_backlog", return_value=task_view(["@CoDeX"])):
            agent_name, argv, suffix = spawn.resolve_agent(config, self.project, "TASK-2")
        self.assertEqual(agent_name, "codex")
        self.assertEqual(argv, ["codex"])
        self.assertIsNone(suffix)

    def test_backlog_cli_failure_falls_back_to_default_agent(self):
        config = self._config({"claude": ["claude"], "codex": ["codex"]})
        with mock.patch.object(server, "run_backlog", side_effect=server.BacklogError("boom")):
            agent_name, argv, suffix = spawn.resolve_agent(config, self.project, "TASK-2")
        self.assertEqual(agent_name, "claude")
        self.assertEqual(argv, ["claude"])
        self.assertIsNone(suffix)

    def test_pre_fetched_task_is_reused_without_a_second_backlog_call(self):
        # task-41: spawn()/resume() fetch the task once (for the
        # pre-claim Done-status check) and pass it through here so
        # resolve_agent doesn't make a second, redundant CLI call.
        config = self._config({"claude": ["claude"], "codex": ["codex"]})
        prefetched = task_view(["@codex"])["task"]
        with mock.patch.object(server, "run_backlog") as run_backlog:
            agent_name, argv, suffix = spawn.resolve_agent(config, self.project, "TASK-2", task=prefetched)
        run_backlog.assert_not_called()
        self.assertEqual(agent_name, "codex")
        self.assertEqual(argv, ["codex"])
        self.assertIsNone(suffix)

    def test_missing_agents_and_defaultAgent_keys_use_seed_defaults(self):
        # A minimal config (like the existing test helper make_config())
        # that doesn't set 'agents'/'defaultAgent' at all.
        config = {"port": 0, "worktreeRoot": "/tmp", "projects": [self.project]}
        with mock.patch.object(server, "run_backlog", return_value=task_view(["@codex"])):
            agent_name, argv, suffix = spawn.resolve_agent(config, self.project, "TASK-2")
        self.assertEqual(agent_name, "codex")
        self.assertEqual(argv, ["codex"])
        self.assertIsNone(suffix)

    def test_object_form_agent_resolves_cmd_and_promptSuffix(self):
        config = self._config({
            "claude": ["claude"],
            "codex": {
                "cmd": ["claude", "--append-system-prompt", "you are codex-flavored"],
                "promptSuffix": "Use the layout-reviewer subagent for review passes.",
            },
        })
        with mock.patch.object(server, "run_backlog", return_value=task_view(["@codex"])):
            agent_name, argv, suffix = spawn.resolve_agent(config, self.project, "TASK-2")
        self.assertEqual(agent_name, "codex")
        self.assertEqual(argv, ["claude", "--append-system-prompt", "you are codex-flavored"])
        self.assertEqual(suffix, "Use the layout-reviewer subagent for review passes.")

    def test_object_form_without_promptSuffix_yields_none(self):
        config = self._config({"claude": {"cmd": ["claude", "--flag"]}})
        with mock.patch.object(server, "run_backlog", return_value=task_view([])):
            agent_name, argv, suffix = spawn.resolve_agent(config, self.project, "TASK-2")
        self.assertEqual(agent_name, "claude")
        self.assertEqual(argv, ["claude", "--flag"])
        self.assertIsNone(suffix)

    def test_mixed_plain_and_object_forms_in_same_map(self):
        config = self._config({
            "claude": ["claude"],
            "wrapper": {"cmd": ["my-wrapper-script"], "promptSuffix": "Follow repo conventions."},
        })
        with mock.patch.object(server, "run_backlog", return_value=task_view(["@Wrapper"])):
            agent_name, argv, suffix = spawn.resolve_agent(config, self.project, "TASK-2")
        self.assertEqual(agent_name, "wrapper")
        self.assertEqual(argv, ["my-wrapper-script"])
        self.assertEqual(suffix, "Follow repo conventions.")


class SpawnAgentSelectionIntegrationTests(unittest.TestCase):
    """Full spawn() runs (no CENTRALE_SPAWN_CMD override) verifying the
    resolved agent drives the tmux argv and comes back in the response."""

    def setUp(self):
        self.config = make_config(
            "/worktrees", [{"name": "my-app", "path": "/repos/my-app"}]
        )
        # Make sure no leftover CENTRALE_SPAWN_CMD from another test process
        # forces the override path here.
        self.env_patch = mock.patch.dict(os.environ, {}, clear=False)
        self.env_patch.start()
        os.environ.pop("CENTRALE_SPAWN_CMD", None)
        self.addCleanup(self.env_patch.stop)

    def _run_spawn(self, config, assignees):
        fake_git = FakeGit(branch_exists=True, worktree_registered=True, wt_dir="/worktrees/my-app-task-2")
        with mock.patch.object(server, "run_git", side_effect=fake_git), \
             mock.patch.object(server, "run_tmux", return_value=tmux_proc([], 0)) as run_tmux, \
             mock.patch.object(server, "list_sessions", return_value=[]), \
             mock.patch.object(server, "run_backlog", return_value=task_view(assignees)), \
             mock.patch.object(server, "run_backlog_raw", return_value=backlog_raw_proc()), \
             mock.patch.object(server, "ensure_hooks_settings_file", return_value="/fake/cache/centrale/hooks-settings.json"), \
             mock.patch.object(server, "probe_codex_hook_trust", return_value=False), \
             mock.patch("os.path.isdir", return_value=True), \
             mock.patch("os.makedirs"):
            result = spawn.spawn(config, "my-app", "TASK-2")
        return result, run_tmux

    def test_codex_assignee_spawns_codex_argv_and_reports_agent(self):
        # config has no 'agents' key at all: seed defaults apply, exactly
        # the plain argv-list form ({"codex": ["codex"]}) — unaffected by
        # the object-form addition.
        result, run_tmux = self._run_spawn(self.config, ["@codex"])
        self.assertEqual(result["agent"], "codex")
        tmux_args = new_session_argv(run_tmux)
        self.assertIn("codex", tmux_args)
        self.assertIn("-e", tmux_args)
        self.assertEqual(tmux_args[tmux_args.index("-e") + 1], "CENTRALE_AGENT=codex")
        self.assertEqual(tmux_args[-1], spawn.prompt_for("TASK-2"))
        self.assertIn(
            f"CENTRALE_EVENT_URL={spawn.event_url(self.config, 'my-app', 'TASK-2', agent_kind='codex')}",
            tmux_args,
        )

    def test_unassigned_task_spawns_claude_argv_and_reports_agent(self):
        result, run_tmux = self._run_spawn(self.config, [])
        self.assertEqual(result["agent"], "claude")
        tmux_args = new_session_argv(run_tmux)
        self.assertIn("claude", tmux_args)
        self.assertEqual(tmux_args[tmux_args.index("-e") + 1], "CENTRALE_AGENT=claude")
        self.assertIn(
            f"CENTRALE_EVENT_URL={spawn.event_url(self.config, 'my-app', 'TASK-2', agent_kind='claude')}",
            tmux_args,
        )

    def test_argv_list_form_end_to_end_through_load_config_and_spawn(self):
        # The plain argv-list form, round-tripped through a real
        # projects.json file and server.load_config, then into spawn() —
        # demonstrates the object-form addition left this form unaffected.
        tmp_path = os.path.join(FIXTURES_DIR, "_tmp_projects_listform.json")
        raw = {
            "worktreeRoot": "/worktrees",
            "agents": {"claude": ["claude"], "codex": ["codex", "exec"]},
            "defaultAgent": "claude",
            "projects": [{"name": "my-app", "path": "/repos/my-app"}],
        }
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(raw, f)
        try:
            config = server.load_config(path=tmp_path)
        finally:
            os.remove(tmp_path)

        result, run_tmux = self._run_spawn(config, ["@codex"])
        self.assertEqual(result["agent"], "codex")
        tmux_args = new_session_argv(run_tmux)
        # cmd argv, then the codex notify hook injection (task-37), then
        # the prompt last -- see test_codex_notify_override_appended_before_prompt
        # for the injection's own dedicated coverage.
        self.assertEqual(tmux_args[-5], "codex")
        self.assertEqual(tmux_args[-4], "exec")
        self.assertEqual(tmux_args[-3], "-c")
        self.assertIn("finished", tmux_args[-2])
        # No promptSuffix on this entry: the trailing arg is exactly the
        # standard prompt, nothing appended.
        self.assertEqual(tmux_args[-1], spawn.prompt_for("TASK-2"))

    def test_object_form_agent_end_to_end_through_load_config_and_spawn(self):
        # The object form, round-tripped through a real projects.json file
        # and server.load_config, then into spawn() — this is the
        # end-to-end path a real user's config takes.
        tmp_path = os.path.join(FIXTURES_DIR, "_tmp_projects_objform.json")
        raw = {
            "worktreeRoot": "/worktrees",
            "agents": {
                "claude": ["claude"],
                "codex": {
                    "cmd": ["codex", "exec"],
                    "promptSuffix": "Use the layout-reviewer subagent for review passes.",
                },
            },
            "defaultAgent": "claude",
            "projects": [{"name": "my-app", "path": "/repos/my-app"}],
        }
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(raw, f)
        try:
            config = server.load_config(path=tmp_path)
        finally:
            os.remove(tmp_path)

        result, run_tmux = self._run_spawn(config, ["@codex"])
        self.assertEqual(result["agent"], "codex")
        tmux_args = new_session_argv(run_tmux)
        self.assertEqual(tmux_args[-5], "codex")
        self.assertEqual(tmux_args[-4], "exec")
        self.assertEqual(tmux_args[-3], "-c")
        self.assertIn("finished", tmux_args[-2])
        expected_prompt = (
            spawn.prompt_for("TASK-2")
            + "\n\nUse the layout-reviewer subagent for review passes."
        )
        self.assertEqual(tmux_args[-1], expected_prompt)

    def _load_config_from(self, filename, raw):
        tmp_path = os.path.join(FIXTURES_DIR, filename)
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(raw, f)
        try:
            return server.load_config(path=tmp_path)
        finally:
            os.remove(tmp_path)

    def test_spawn_prompt_override_is_used_verbatim_and_prompt_suffix_still_layers(self):
        # Task-69: the whole path through load_config -> spawn(): the
        # configured template replaces the default, and the agent's
        # promptSuffix appends to it exactly as it does to the default.
        config = self._load_config_from("_tmp_projects_spawn_prompt.json", {
            "worktreeRoot": "/worktrees",
            "spawnPrompt": "Take backlog task {task_id}. Ship it on this branch only.",
            "agents": {
                "claude": ["claude"],
                "codex": {"cmd": ["codex"], "promptSuffix": "Prefer small commits."},
            },
            "defaultAgent": "claude",
            "projects": [{"name": "my-app", "path": "/repos/my-app"}],
        })
        self.assertEqual(config["spawnPrompt"], "Take backlog task {task_id}. Ship it on this branch only.")

        # promptSuffix layered on the override.
        result, run_tmux = self._run_spawn(config, ["@codex"])
        self.assertEqual(result["agent"], "codex")
        tmux_args = new_session_argv(run_tmux)
        self.assertEqual(
            tmux_args[-1],
            "Take backlog task TASK-2. Ship it on this branch only.\n\nPrefer small commits.",
        )
        self.assertNotIn("Backlog.md workflow", tmux_args[-1])

        # No promptSuffix: the override alone, verbatim.
        result, run_tmux = self._run_spawn(config, ["@claude"])
        self.assertEqual(result["agent"], "claude")
        tmux_args = new_session_argv(run_tmux)
        self.assertEqual(tmux_args[-1], "Take backlog task TASK-2. Ship it on this branch only.")

    def test_shipped_example_config_does_not_freeze_the_spawn_prompt(self):
        # Task-69 shipped the spelled-out default under spawnPrompt so it
        # was visible where users start from. Task-103: that made the
        # example do the one thing the docs advise against -- a present
        # key freezes a copy of the prompt and stops tracking future
        # improvements to it -- for a reader who never asked to override
        # anything. The key is now absent, so copying the example follows
        # the built-in prompt; the example must still load cleanly (the
        # validation path) and resolve to exactly the built-in text.
        example_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                    "projects.example.json")
        with open(example_path, "r", encoding="utf-8") as f:
            self.assertNotIn("spawnPrompt", json.load(f))
        config = server.load_config(path=example_path)
        self.assertIsNone(config["spawnPrompt"])
        self.assertEqual(spawn.prompt_for("TASK-2", config), spawn.prompt_for("TASK-2"))
        self.assertEqual(spawn.prompt_for("TASK-2", config),
                         spawn.PROMPT_TEMPLATE.format(task_id="TASK-2"))

    def test_spawn_prompt_absent_means_the_built_in_default(self):
        config = self._load_config_from("_tmp_projects_no_spawn_prompt.json", {
            "worktreeRoot": "/worktrees",
            "projects": [{"name": "my-app", "path": "/repos/my-app"}],
        })
        self.assertIsNone(config["spawnPrompt"])
        _, run_tmux = self._run_spawn(config, ["@codex"])
        tmux_args = new_session_argv(run_tmux)
        self.assertEqual(tmux_args[-1], spawn.PROMPT_TEMPLATE.format(task_id="TASK-2"))
        self.assertIn("`backlog decision list --plain`", tmux_args[-1])

    def test_spawn_prompt_missing_task_id_placeholder_fails_config_load(self):
        # Task-69: same strictness as a malformed agents entry -- refused
        # at startup with a clear message, never a task-less spawn later.
        for bad in ("Work on the task and commit.", "", "   ", 42, ["{task_id}"]):
            with self.subTest(bad=bad):
                with self.assertRaises(server.ConfigError) as ctx:
                    self._load_config_from("_tmp_projects_bad_spawn_prompt.json", {
                        "spawnPrompt": bad,
                        "projects": [{"name": "my-app", "path": "/repos/my-app"}],
                    })
                self.assertIn("spawnPrompt", str(ctx.exception))
                self.assertIn("{task_id}", str(ctx.exception))

    def test_malformed_agents_entry_is_rejected_at_load_time_before_spawn_is_reachable(self):
        # A malformed 'agents' entry in projects.json must fail loudly at
        # server startup (server.load_config), long before /api/spawn (and
        # therefore spawn.spawn) could ever be reached with it.
        tmp_path = os.path.join(FIXTURES_DIR, "_tmp_projects_malformed.json")
        raw = {
            "agents": {"claude": ["claude"], "bogus": {"promptSuffix": "no cmd here"}},
            "projects": [{"name": "my-app", "path": "/repos/my-app"}],
        }
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(raw, f)
        try:
            with mock.patch.object(server, "run_git") as run_git, \
                 mock.patch.object(server, "run_tmux") as run_tmux, \
                 mock.patch.object(server, "run_backlog") as run_backlog:
                with self.assertRaises(server.ConfigError) as ctx:
                    server.load_config(path=tmp_path)
                run_git.assert_not_called()
                run_tmux.assert_not_called()
                run_backlog.assert_not_called()
        finally:
            os.remove(tmp_path)
        self.assertIn("bogus", str(ctx.exception))
        self.assertIn("cmd", str(ctx.exception))

    def test_spawn_cmd_override_wins_and_skips_backlog_lookup(self):
        with mock.patch.dict(os.environ, {"CENTRALE_SPAWN_CMD": "sleep 300"}):
            fake_git = FakeGit(branch_exists=True, worktree_registered=True, wt_dir="/worktrees/my-app-task-2")
            with mock.patch.object(server, "run_git", side_effect=fake_git), \
                 mock.patch.object(server, "run_tmux", return_value=tmux_proc([], 0)) as run_tmux, \
                 mock.patch.object(server, "list_sessions", return_value=[]), \
                 mock.patch.object(server, "run_backlog") as run_backlog, \
                 mock.patch.object(server, "run_backlog_raw", return_value=backlog_raw_proc()) as run_backlog_raw, \
                 mock.patch("os.path.isdir", return_value=True), \
                 mock.patch("os.makedirs"):
                result = spawn.spawn(self.config, "my-app", "TASK-2")

        # The override skips assignee-driven agent resolution (run_backlog,
        # the JSON-returning `task view` boundary)...
        run_backlog.assert_not_called()
        # ...but not the claim-and-commit workflow step (run_backlog_raw,
        # the `task edit` boundary) -- that runs regardless of the override.
        run_backlog_raw.assert_called_once_with(
            ["task", "edit", "TASK-2", "-s", "In Progress"], cwd="/repos/my-app"
        )
        self.assertEqual(result["agent"], "custom")
        tmux_args = new_session_argv(run_tmux)
        # No CENTRALE_AGENT under the override (agent_name stays None) and
        # no argv hook injection either (agent resolution itself is
        # skipped) -- but CENTRALE_EVENT_URL is still set unconditionally
        # (task-37 AC #4), so exactly one "-e" pair shows up here.
        self.assertEqual(tmux_args.count("-e"), 1)
        self.assertNotIn("CENTRALE_AGENT=", "".join(tmux_args))
        self.assertEqual(
            tmux_args,
            [
                "new-session", "-d", "-s", "centrale-my-app-task-2",
                "-c", "/worktrees/my-app-task-2", *geometry_args(),
                "-e", f"CENTRALE_EVENT_URL={spawn.event_url(self.config, 'my-app', 'TASK-2')}",
                "sleep", "300", spawn.prompt_for("TASK-2"),
            ],
        )


class SpawnHappyPathTests(unittest.TestCase):
    def setUp(self):
        self.config = make_config(
            "/worktrees", [{"name": "my-app", "path": "/repos/my-app"}]
        )
        self.env_patch = mock.patch.dict(os.environ, {"CENTRALE_SPAWN_CMD": "sleep 300"})
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)

    def test_happy_path_creates_worktree_and_tmux_session(self):
        fake_git = FakeGit(branch_exists=False, worktree_registered=False)
        makedirs_calls = []

        with mock.patch.object(server, "run_git", side_effect=fake_git), \
             mock.patch.object(server, "run_tmux", return_value=tmux_proc([], 0)) as run_tmux, \
             mock.patch.object(server, "list_sessions", return_value=[]), \
             mock.patch.object(server, "run_backlog_raw", return_value=backlog_raw_proc()), \
             mock.patch("os.path.isdir", return_value=False), \
             mock.patch("os.makedirs", side_effect=lambda *a, **k: makedirs_calls.append(a)):
            result = spawn.spawn(self.config, "my-app", "TASK-2")

        self.assertEqual(result["session"], "centrale-my-app-task-2")
        self.assertEqual(result["attach"], "tmux attach -t centrale-my-app-task-2")
        self.assertNotIn("warnings", result)

        # A `git worktree add -b` call happened (no existing branch/worktree).
        add_calls = [c for c in fake_git.calls if c[:2] == ["worktree", "add"]]
        self.assertEqual(len(add_calls), 1)
        self.assertIn("-b", add_calls[0])

        # tmux argv assertions (exact shape; see docs/architecture.md).
        tmux_args = new_session_argv(run_tmux)
        expected_wt_dir = "/worktrees/my-app-task-2"
        self.assertEqual(tmux_args[0], "new-session")
        self.assertIn("-d", tmux_args)
        self.assertIn("-s", tmux_args)
        self.assertEqual(tmux_args[tmux_args.index("-s") + 1], "centrale-my-app-task-2")
        self.assertIn("-c", tmux_args)
        self.assertEqual(tmux_args[tmux_args.index("-c") + 1], expected_wt_dir)
        # spawn command argv followed by the prompt as the final arg.
        self.assertEqual(tmux_args[-3], "sleep")
        self.assertEqual(tmux_args[-2], "300")
        self.assertEqual(tmux_args[-1], spawn.prompt_for("TASK-2"))
        self.assertEqual(
            tmux_args,
            [
                "new-session", "-d", "-s", "centrale-my-app-task-2",
                "-c", expected_wt_dir, *geometry_args(),
                "-e", f"CENTRALE_EVENT_URL={spawn.event_url(self.config, 'my-app', 'TASK-2')}",
                "sleep", "300", spawn.prompt_for("TASK-2"),
            ],
        )

    def test_dotted_subtask_id_gets_tmux_safe_session_but_dotted_worktree(self):
        # task-59 AC #1: a dotted subtask id (TASK-11.2) must produce a
        # session name tmux accepts verbatim (underscore-encoded) and a
        # surfaced attach command using that exact encoded name (task-59
        # comment #1), while the worktree directory it's launched from
        # keeps the dot -- filesystem paths allow "." natively and an
        # existing worktree for this subtask must still resolve.
        fake_git = FakeGit(branch_exists=False, worktree_registered=False)

        with mock.patch.object(server, "run_git", side_effect=fake_git), \
             mock.patch.object(server, "run_tmux", return_value=tmux_proc([], 0)) as run_tmux, \
             mock.patch.object(server, "list_sessions", return_value=[]), \
             mock.patch.object(server, "run_backlog_raw", return_value=backlog_raw_proc()), \
             mock.patch("os.path.isdir", return_value=False), \
             mock.patch("os.makedirs"):
            result = spawn.spawn(self.config, "my-app", "TASK-11.2")

        self.assertEqual(result["session"], "centrale-my-app-task-11_2")
        self.assertEqual(result["attach"], "tmux attach -t centrale-my-app-task-11_2")

        tmux_args = new_session_argv(run_tmux)
        self.assertEqual(tmux_args[tmux_args.index("-s") + 1], "centrale-my-app-task-11_2")
        self.assertEqual(tmux_args[tmux_args.index("-c") + 1], "/worktrees/my-app-task-11.2")


class SessionGeometryTests(unittest.TestCase):
    """task-113: every session Centrale creates is born at
    SESSION_GEOMETRY and holds that size for life.

    An agent TUI runs on tmux's alternate screen, so nothing ever
    accumulates in the pane's scrollback and `capture-pane -S -200` can
    only ever return the pane's visible height. The theater's window
    therefore isn't bounded by what it asks for -- it's bounded by how
    tall the pane is, which is what these tests pin down.
    """

    def setUp(self):
        self.config = make_config(
            "/worktrees", [{"name": "my-app", "path": "/repos/my-app"}]
        )
        self.env_patch = mock.patch.dict(os.environ, {"CENTRALE_SPAWN_CMD": "sleep 300"})
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)

    def _launch(self, resume=False, tmux=None, sessions=None):
        """Run spawn() (or resume()) against a fully mocked world and
        return the patched run_tmux. `tmux` overrides what run_tmux
        answers, so a tmux that rejects `window-size` can be simulated."""
        fake_git = FakeGit(branch_exists=True, worktree_registered=True, wt_dir="/worktrees/my-app-task-2")
        kwargs = {"side_effect": tmux} if tmux else {"return_value": tmux_proc([], 0)}
        with mock.patch.object(server, "run_git", side_effect=fake_git),              mock.patch.object(server, "run_tmux", **kwargs) as run_tmux,              mock.patch.object(server, "list_sessions", return_value=sessions or []),              mock.patch.object(server, "run_backlog_raw", return_value=backlog_raw_proc()),              mock.patch("os.path.isdir", return_value=True),              mock.patch("os.makedirs"):
            if resume:
                spawn.resume(self.config, "my-app", "TASK-2")
            else:
                spawn.spawn(self.config, "my-app", "TASK-2")
        return run_tmux

    def test_geometry_constant_is_bigger_than_tmux_default_size(self):
        # 80x24 is tmux's own `default-size`, which is what a detached
        # `new-session` with no -x/-y inherits -- the state this task
        # exists to leave behind. The constant is only worth having if
        # it clears that, and clears MAX_SESSION_PANE_LINES' floor of
        # DEFAULT_SESSION_PANE_LINES so the theater's default ask is
        # actually satisfiable from the visible screen alone.
        columns, rows = spawn.SESSION_GEOMETRY
        self.assertGreater(columns, 80)
        self.assertGreater(rows, 24)
        self.assertGreaterEqual(rows, server.DEFAULT_SESSION_PANE_LINES)

    def test_the_pane_is_exactly_as_tall_as_the_capture_ceiling(self):
        # task-152, AC #4: the theater asks /api/session-pane for
        # MAX_SESSION_PANE_LINES, and an alternate-screen agent TUI can
        # only ever answer with the pane's height -- so a pane shorter
        # than that ceiling is a request for lines that can never
        # arrive. One number, not two that have to be kept in step by
        # hand: the row count IS the ceiling.
        self.assertEqual(spawn.SESSION_GEOMETRY[1], server.MAX_SESSION_PANE_LINES)

    def test_spawn_creates_the_session_at_the_geometry_constant(self):
        tmux_args = new_session_argv(self._launch())
        self.assertEqual(tmux_args[tmux_args.index("-x") + 1], str(spawn.SESSION_GEOMETRY[0]))
        self.assertEqual(tmux_args[tmux_args.index("-y") + 1], str(spawn.SESSION_GEOMETRY[1]))
        # Flags, so they must precede the launched command and its prompt.
        self.assertLess(tmux_args.index("-y"), tmux_args.index("sleep"))

    def test_resume_creates_the_session_at_the_same_geometry_constant(self):
        # AC #1: the resume path is a session-creation path too, and the
        # theater can't tell the two apart.
        tmux_args = new_session_argv(self._launch(resume=True))
        self.assertEqual(tmux_args[tmux_args.index("-x") + 1], str(spawn.SESSION_GEOMETRY[0]))
        self.assertEqual(tmux_args[tmux_args.index("-y") + 1], str(spawn.SESSION_GEOMETRY[1]))

    def test_both_paths_build_their_new_session_argv_from_one_helper(self):
        # The guarantee behind AC #1: the two creation sites can't drift
        # apart, because they share the argv prefix.
        spawn_args = new_session_argv(self._launch())
        resume_args = new_session_argv(self._launch(resume=True))
        prefix = spawn.new_session_args("centrale-my-app-task-2", "/worktrees/my-app-task-2")
        self.assertEqual(spawn_args[:len(prefix)], prefix)
        self.assertEqual(resume_args[:len(prefix)], prefix)

    def test_spawn_pins_window_size_manual_on_the_session_it_just_created(self):
        # Without this the -x/-y above is only a starting size: with
        # window-size at its default (`latest`), the first `tmux attach`
        # from a smaller terminal shrinks the window and it stays shrunk
        # after that client detaches.
        calls = [c.args[0] for c in self._launch().call_args_list]
        self.assertEqual(
            [c for c in calls if c[0] == "set-option"],
            [["set-option", "-t", "=centrale-my-app-task-2:", "window-size", "manual"]],
        )
        # The target uses the same exact-match discipline as
        # capture_session_pane -- the trailing ":" makes "=" an exact
        # SESSION-name match, so "...-task-1" can never resolve to
        # "...-task-10" -- and it lands after the session exists.
        self.assertEqual(calls[0][0], "new-session")
        self.assertEqual(calls[1][0], "set-option")

    def test_resume_pins_window_size_manual_too(self):
        calls = [c.args[0] for c in self._launch(resume=True).call_args_list]
        self.assertIn(
            ["set-option", "-t", "=centrale-my-app-task-2:", "window-size", "manual"], calls
        )

    def test_spawn_resizes_the_new_session_behind_the_pin(self):
        # task-152: the pin alone is not enough, and on the machine this
        # runs on it is actively wrong. `window-size latest` is
        # inherited globally and resolved when the session is BORN, so
        # with any client attached to the tmux server the -x/-y is
        # overridden before hold_session_geometry() gets to run, and
        # `manual` then freezes that wrong size. The explicit resize
        # behind the pin is what actually lands the geometry.
        columns, rows = spawn.SESSION_GEOMETRY
        calls = [c.args[0] for c in self._launch().call_args_list]
        self.assertEqual(
            [c for c in calls if c[0] == "resize-window"],
            [["resize-window", "-t", "=centrale-my-app-task-2:", "-x", str(columns), "-y", str(rows)]],
        )
        # Order is the whole point: create, pin, then resize. Resizing
        # before the pin would be undone by the pin's own resolution.
        self.assertEqual([c[0] for c in calls[:3]], ["new-session", "set-option", "resize-window"])

    def test_resume_resizes_behind_the_pin_too(self):
        # The resume path creates a session exactly like spawn does, and
        # the theater cannot tell the two apart.
        columns, rows = spawn.SESSION_GEOMETRY
        calls = [c.args[0] for c in self._launch(resume=True).call_args_list]
        self.assertIn(
            ["resize-window", "-t", "=centrale-my-app-task-2:", "-x", str(columns), "-y", str(rows)],
            calls,
        )

    def test_the_resize_target_is_an_exact_session_name_like_the_pin(self):
        # Same discipline as capture_session_pane and the pin above: a
        # leading "=" with a trailing ":" is an exact SESSION-name
        # match, so "...-task-1" can never resize "...-task-10".
        calls = [c.args[0] for c in self._launch().call_args_list]
        resize = next(c for c in calls if c[0] == "resize-window")
        self.assertEqual(resize[resize.index("-t") + 1], "=centrale-my-app-task-2:")

    def test_nothing_resizes_a_session_that_already_exists(self):
        # AC #4: a live agent's pane is never resized out from under it.
        # Other sessions are running here; the only session named in any
        # tmux call is the one this spawn just created.
        names = ["centrale-my-app-task-7", "centrale-other-task-1"]
        sessions = [{"name": n} for n in names]
        calls = [c.args[0] for c in self._launch(sessions=sessions).call_args_list]
        for name in names:
            self.assertNotIn(name, " ".join(a for c in calls for a in c))
        # task-152 added a resize, so "no resize at all" is no longer
        # the invariant -- "every resize names the session this spawn
        # just created" is, and it is the one that protects a live
        # agent's pane from being resized out from under it.
        self.assertEqual(
            [c[c.index("-t") + 1] for c in calls if c[0].startswith("resize-")],
            ["=centrale-my-app-task-2:"],
        )

    def test_a_tmux_that_rejects_window_size_still_spawns(self):
        # `window-size` arrived in tmux 3.0. The session and its agent
        # are already live by the time it's set, so an older tmux costs
        # a pane that shrinks on attach -- not a failed spawn.
        def tmux(args):
            if args[0] == "set-option":
                return tmux_proc(args, 1, stderr="unknown option: window-size")
            return tmux_proc(args, 0)

        run_tmux = self._launch(tmux=tmux)
        new_session_argv(run_tmux)  # the spawn itself completed
        # And the resize is still attempted: an unpinned session at the
        # right size beats a pinned one at the wrong size.
        calls = [c.args[0] for c in run_tmux.call_args_list]
        self.assertTrue(any(c[0] == "resize-window" for c in calls))

    def test_a_tmux_that_rejects_the_resize_still_spawns(self):
        # task-152, AC #2: the resize is best-effort in exactly the way
        # the pin is. The session and its agent are already live, so a
        # tmux without `resize-window` costs a pane the size its birth
        # gave it, never a 500 on a spawn that already happened.
        def tmux(args):
            if args[0] == "resize-window":
                return tmux_proc(args, 1, stderr="unknown command: resize-window")
            return tmux_proc(args, 0)

        run_tmux = self._launch(tmux=tmux)
        new_session_argv(run_tmux)  # the spawn itself completed

    def test_neither_the_pin_nor_the_resize_can_fail_a_spawn(self):
        # Both gone at once -- the pre-2.9 tmux case -- is still a
        # successful spawn with a working session.
        def tmux(args):
            if args[0] in ("set-option", "resize-window"):
                return tmux_proc(args, 1, stderr="unknown command")
            return tmux_proc(args, 0)

        for resume in (False, True):
            with self.subTest(resume=resume):
                new_session_argv(self._launch(resume=resume, tmux=tmux))

    def test_a_failed_new_session_never_reaches_set_option_or_resize(self):
        # Nothing to pin and nothing to resize: the session was never
        # created, and the 500 the caller gets must not be masked by a
        # second tmux failure.
        seen = []

        def tmux(args):
            seen.append(args[0])
            return tmux_proc(args, 1, stderr="boom")

        with self.assertRaises(spawn.SpawnError) as caught:
            self._launch(tmux=tmux)
        self.assertEqual(caught.exception.status, 500)
        self.assertEqual(seen, ["new-session"])


class AgentEventResidueResetTests(unittest.TestCase):
    """A new tmux session starts clean without racing its first hook."""

    def setUp(self):
        self.config = make_config(
            "/worktrees", [{"name": "my-app", "path": "/repos/my-app"}]
        )
        self.env_patch = mock.patch.dict(os.environ, {"CENTRALE_SPAWN_CMD": "sleep 300"})
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)
        server._reset_agent_events()
        self.addCleanup(server._reset_agent_events)

    def _common_patches(self, run_tmux):
        fake_git = FakeGit(
            branch_exists=True,
            worktree_registered=True,
            wt_dir="/worktrees/my-app-task-2",
        )
        return (
            mock.patch.object(server, "run_git", side_effect=fake_git),
            mock.patch.object(server, "run_tmux", side_effect=run_tmux),
            mock.patch.object(server, "list_sessions", return_value=[]),
            mock.patch.object(server, "run_backlog_raw", return_value=backlog_raw_proc()),
            mock.patch("os.path.isdir", return_value=True),
            mock.patch("os.makedirs"),
        )

    def test_spawn_clears_waiting_residue_before_launch_not_after(self):
        server.record_agent_event("my-app", "TASK-2", "waiting")

        def launch(args):
            # Only the launch itself is under test; task-113's follow-up
            # `set-option window-size manual` runs through the same
            # patched run_tmux and must not re-run these assertions.
            if args[0] != "new-session":
                return tmux_proc(args, 0)
            self.assertEqual(server.get_agent_state("my-app", "TASK-2"), "unknown")
            # Simulate the detached process posting its first event before
            # run_tmux returns. A post-launch clear would wrongly erase it.
            server.record_agent_event("my-app", "TASK-2", "working")
            return tmux_proc(args, 0)

        patches = self._common_patches(launch)
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
            spawn.spawn(self.config, "my-app", "TASK-2")

        self.assertEqual(server.get_agent_state("my-app", "TASK-2"), "working")

    def test_resume_clears_working_residue_before_launch_not_after(self):
        server.record_agent_event("my-app", "TASK-2", "working")

        def launch(args):
            if args[0] != "new-session":  # see above
                return tmux_proc(args, 0)
            self.assertEqual(server.get_agent_state("my-app", "TASK-2"), "unknown")
            server.record_agent_event("my-app", "TASK-2", "waiting")
            return tmux_proc(args, 0)

        patches = self._common_patches(launch)
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
            spawn.resume(self.config, "my-app", "TASK-2")

        self.assertEqual(server.get_agent_state("my-app", "TASK-2"), "waiting")


class SpawnValidationTests(unittest.TestCase):
    def setUp(self):
        self.config = make_config(
            "/worktrees", [{"name": "my-app", "path": "/repos/my-app"}]
        )

    def _assert_no_subprocess_calls(self, run_git, run_tmux):
        run_git.assert_not_called()
        run_tmux.assert_not_called()

    def test_tmux_unavailable_rejected_before_any_subprocess_call(self):
        config = make_config(
            "/worktrees", [{"name": "my-app", "path": "/repos/my-app"}]
        )
        config["capabilities"] = {"tmux": False}
        with mock.patch.object(server, "run_git") as run_git, \
             mock.patch.object(server, "run_tmux") as run_tmux, \
             mock.patch.object(server, "list_sessions") as list_sessions:
            with self.assertRaises(spawn.SpawnError) as ctx:
                spawn.spawn(config, "my-app", "TASK-2")
            self.assertEqual(ctx.exception.status, 503)
            self.assertIn("tmux", str(ctx.exception).lower())
            self._assert_no_subprocess_calls(run_git, run_tmux)
            list_sessions.assert_not_called()

    def test_unknown_project_rejected(self):
        with mock.patch.object(server, "run_git") as run_git, \
             mock.patch.object(server, "run_tmux") as run_tmux, \
             mock.patch.object(server, "list_sessions") as list_sessions:
            with self.assertRaises(spawn.SpawnError) as ctx:
                spawn.spawn(self.config, "nonexistent", "TASK-2")
            self.assertGreaterEqual(ctx.exception.status, 400)
            self.assertLess(ctx.exception.status, 500)
            self._assert_no_subprocess_calls(run_git, run_tmux)
            list_sessions.assert_not_called()

    def test_malformed_task_ids_rejected_before_any_subprocess_call(self):
        bad_ids = ["../etc", "TASK", "TASK-", "foo bar", "TASK-1; rm -rf /"]
        for bad_id in bad_ids:
            with self.subTest(bad_id=bad_id):
                with mock.patch.object(server, "run_git") as run_git, \
                     mock.patch.object(server, "run_tmux") as run_tmux, \
                     mock.patch.object(server, "list_sessions") as list_sessions:
                    with self.assertRaises(spawn.SpawnError) as ctx:
                        spawn.spawn(self.config, "my-app", bad_id)
                    self.assertGreaterEqual(ctx.exception.status, 400)
                    self.assertLess(ctx.exception.status, 500)
                    self._assert_no_subprocess_calls(run_git, run_tmux)
                    list_sessions.assert_not_called()

    def test_duplicate_session_returns_409(self):
        with mock.patch.object(server, "run_git") as run_git, \
             mock.patch.object(server, "run_tmux") as run_tmux, \
             mock.patch.object(
                 server, "list_sessions",
                 return_value=[{"name": "centrale-my-app-task-2", "created": "0", "attached": False}],
             ):
            with self.assertRaises(spawn.SpawnError) as ctx:
                spawn.spawn(self.config, "my-app", "TASK-2")
            self.assertEqual(ctx.exception.status, 409)
            run_git.assert_not_called()
            run_tmux.assert_not_called()

    def test_duplicate_session_returns_409_for_dotted_subtask_id(self):
        # task-59: re-spawning a live dotted-id task must hit this clean
        # 409 (from list_sessions() already reporting the tmux-mangled
        # "_2" name), not fall through to a raw tmux "duplicate session"
        # error from actually attempting new-session.
        with mock.patch.object(server, "run_git") as run_git, \
             mock.patch.object(server, "run_tmux") as run_tmux, \
             mock.patch.object(
                 server, "list_sessions",
                 return_value=[{"name": "centrale-my-app-task-11_2", "created": "0", "attached": False}],
             ):
            with self.assertRaises(spawn.SpawnError) as ctx:
                spawn.spawn(self.config, "my-app", "TASK-11.2")
            self.assertEqual(ctx.exception.status, 409)
            run_git.assert_not_called()
            run_tmux.assert_not_called()


class WorktreeReuseTests(unittest.TestCase):
    def setUp(self):
        self.config = make_config(
            "/worktrees", [{"name": "my-app", "path": "/repos/my-app"}]
        )
        self.wt_dir = "/worktrees/my-app-task-2"
        self.env_patch = mock.patch.dict(os.environ, {"CENTRALE_SPAWN_CMD": "sleep 300"})
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)

    def _run_spawn(self, fake_git, isdir_return):
        with mock.patch.object(server, "run_git", side_effect=fake_git), \
             mock.patch.object(server, "run_tmux", return_value=tmux_proc([], 0)), \
             mock.patch.object(server, "list_sessions", return_value=[]), \
             mock.patch.object(server, "run_backlog_raw", return_value=backlog_raw_proc()), \
             mock.patch("os.path.isdir", return_value=isdir_return), \
             mock.patch("os.makedirs"):
            return spawn.spawn(self.config, "my-app", "TASK-2")

    def test_existing_worktree_and_registered_skips_worktree_add(self):
        fake_git = FakeGit(
            branch_exists=True, worktree_registered=True, wt_dir=self.wt_dir
        )
        self._run_spawn(fake_git, isdir_return=True)

        add_calls = [c for c in fake_git.calls if c[:2] == ["worktree", "add"]]
        self.assertEqual(add_calls, [], "no `git worktree add` should run when the worktree is already registered")

    def test_existing_branch_missing_worktree_adds_without_dash_b(self):
        fake_git = FakeGit(
            branch_exists=True, worktree_registered=False, wt_dir=self.wt_dir
        )
        self._run_spawn(fake_git, isdir_return=False)

        add_calls = [c for c in fake_git.calls if c[:2] == ["worktree", "add"]]
        self.assertEqual(len(add_calls), 1)
        self.assertNotIn("-b", add_calls[0])
        self.assertIn(self.wt_dir, add_calls[0])
        self.assertIn("task/task-2", add_calls[0])

    def test_neither_branch_nor_worktree_exists_adds_with_dash_b(self):
        fake_git = FakeGit(
            branch_exists=False, worktree_registered=False, wt_dir=self.wt_dir
        )
        self._run_spawn(fake_git, isdir_return=False)

        add_calls = [c for c in fake_git.calls if c[:2] == ["worktree", "add"]]
        self.assertEqual(len(add_calls), 1)
        self.assertIn("-b", add_calls[0])
        self.assertIn("task/task-2", add_calls[0])
        self.assertIn(self.wt_dir, add_calls[0])

    def test_git_worktree_add_failure_becomes_spawn_error_500(self):
        fake_git = FakeGit(branch_exists=False, worktree_registered=False, wt_dir=self.wt_dir)

        def failing_git(args, cwd=None):
            if args[:2] == ["worktree", "add"]:
                return git_proc(args, 128, "", "fatal: something went wrong")
            return fake_git(args, cwd=cwd)

        with mock.patch.object(server, "run_git", side_effect=failing_git), \
             mock.patch.object(server, "run_tmux") as run_tmux, \
             mock.patch.object(server, "list_sessions", return_value=[]), \
             mock.patch.object(server, "run_backlog_raw", return_value=backlog_raw_proc()), \
             mock.patch("os.path.isdir", return_value=False), \
             mock.patch("os.makedirs"):
            with self.assertRaises(spawn.SpawnError) as ctx:
                spawn.spawn(self.config, "my-app", "TASK-2")
        self.assertEqual(ctx.exception.status, 500)
        self.assertIn("something went wrong", str(ctx.exception))
        run_tmux.assert_not_called()

    def test_tmux_failure_becomes_spawn_error_500(self):
        fake_git = FakeGit(branch_exists=True, worktree_registered=True, wt_dir=self.wt_dir)
        with mock.patch.object(server, "run_git", side_effect=fake_git), \
             mock.patch.object(server, "run_tmux", return_value=tmux_proc([], 1, "", "tmux: failed")), \
             mock.patch.object(server, "list_sessions", return_value=[]), \
             mock.patch.object(server, "run_backlog_raw", return_value=backlog_raw_proc()), \
             mock.patch("os.path.isdir", return_value=True), \
             mock.patch("os.makedirs"):
            with self.assertRaises(spawn.SpawnError) as ctx:
                spawn.spawn(self.config, "my-app", "TASK-2")
        self.assertEqual(ctx.exception.status, 500)
        self.assertIn("tmux: failed", str(ctx.exception))


class InRepoWorktreeSpawnTests(unittest.TestCase):
    """End-to-end: spawning with worktreeRoot="@repo" against a real
    (temp) repo directory actually creates the worktree dir under
    <repo>/.centrale-worktrees and writes the .git/info/exclude entry --
    proving _ensure_worktree's makedirs/exclude wiring, not just the
    path-resolution math WorktreeRootResolutionTests covers in
    isolation. git/tmux themselves stay mocked, same as every other
    spawn test; only the plain filesystem calls (os.makedirs, the
    exclude file) are real, against this temp dir."""

    def setUp(self):
        import tempfile
        self.repo = tempfile.mkdtemp(prefix="centrale-test-inrepo-")
        self.addCleanup(self._cleanup)
        self.config = make_config("@repo", [{"name": "my-app", "path": self.repo}])
        self.expected_wt_dir = os.path.join(self.repo, ".centrale-worktrees", "my-app-task-2")
        self.env_patch = mock.patch.dict(os.environ, {"CENTRALE_SPAWN_CMD": "sleep 300"})
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)

    def _cleanup(self):
        import shutil
        shutil.rmtree(self.repo, ignore_errors=True)

    def test_worktree_created_inside_the_repo(self):
        fake_git = FakeGit(branch_exists=False, worktree_registered=False, wt_dir=self.expected_wt_dir)
        with mock.patch.object(server, "run_git", side_effect=fake_git), \
             mock.patch.object(server, "run_tmux", return_value=tmux_proc([], 0)), \
             mock.patch.object(server, "list_sessions", return_value=[]), \
             mock.patch.object(server, "run_backlog_raw", return_value=backlog_raw_proc()):
            spawn.spawn(self.config, "my-app", "TASK-2")

        add_calls = [c for c in fake_git.calls if c[:2] == ["worktree", "add"]]
        self.assertEqual(len(add_calls), 1)
        self.assertIn(self.expected_wt_dir, add_calls[0])
        # os.makedirs was real here (not mocked) -- the worktree root dir
        # must actually exist inside the repo now.
        self.assertTrue(os.path.isdir(os.path.join(self.repo, ".centrale-worktrees")))

    def test_git_info_exclude_gets_the_worktree_root_entry(self):
        fake_git = FakeGit(branch_exists=False, worktree_registered=False, wt_dir=self.expected_wt_dir)
        with mock.patch.object(server, "run_git", side_effect=fake_git), \
             mock.patch.object(server, "run_tmux", return_value=tmux_proc([], 0)), \
             mock.patch.object(server, "list_sessions", return_value=[]), \
             mock.patch.object(server, "run_backlog_raw", return_value=backlog_raw_proc()):
            spawn.spawn(self.config, "my-app", "TASK-2")

        exclude_path = os.path.join(self.repo, ".git", "info", "exclude")
        with open(exclude_path, "r", encoding="utf-8") as f:
            lines = f.read().splitlines()
        self.assertIn(".centrale-worktrees/", lines)

    def test_resume_into_an_existing_in_repo_worktree_does_not_duplicate_the_exclude_entry(self):
        # First spawn creates the worktree + the exclude entry...
        fake_git = FakeGit(branch_exists=False, worktree_registered=False, wt_dir=self.expected_wt_dir)
        with mock.patch.object(server, "run_git", side_effect=fake_git), \
             mock.patch.object(server, "run_tmux", return_value=tmux_proc([], 0)), \
             mock.patch.object(server, "list_sessions", return_value=[]), \
             mock.patch.object(server, "run_backlog_raw", return_value=backlog_raw_proc()):
            spawn.spawn(self.config, "my-app", "TASK-2")

        # ...then resume() reuses it (registered=True now) -- must not
        # add a second identical line to .git/info/exclude.
        os.makedirs(self.expected_wt_dir, exist_ok=True)
        fake_git_reuse = FakeGit(branch_exists=True, worktree_registered=True, wt_dir=self.expected_wt_dir)
        with mock.patch.object(server, "run_git", side_effect=fake_git_reuse), \
             mock.patch.object(server, "run_tmux", return_value=tmux_proc([], 0)), \
             mock.patch.object(server, "list_sessions", return_value=[]):
            spawn.resume(self.config, "my-app", "TASK-2")

        exclude_path = os.path.join(self.repo, ".git", "info", "exclude")
        with open(exclude_path, "r", encoding="utf-8") as f:
            lines = f.read().splitlines()
        self.assertEqual(lines.count(".centrale-worktrees/"), 1)


class SpawnClaimAndCommitTests(unittest.TestCase):
    """Hermetic tests for _claim_and_commit and its integration into
    spawn(): claim-then-commit must precede worktree creation, a clean
    backlog/ tree skips the commit silently, and either step failing
    degrades to a response 'warnings' entry rather than blocking spawn.
    """

    def setUp(self):
        self.config = make_config(
            "/worktrees", [{"name": "my-app", "path": "/repos/my-app"}]
        )
        self.env_patch = mock.patch.dict(os.environ, {"CENTRALE_SPAWN_CMD": "sleep 300"})
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)

    def _run(self, claim_returncode=0, status_stdout="", add_returncode=0, commit_returncode=0):
        """Spawns with every git/backlog call recorded, in order, into a
        single shared list so ordering can be asserted across both
        boundaries. Uses a fresh branch+worktree (neither exists yet) so
        an actual `git worktree add` call happens to order against."""
        calls = []
        fake_git_inner = FakeGit(branch_exists=False, worktree_registered=False)

        def fake_run_backlog_raw(args, cwd):
            calls.append(("backlog_raw", list(args)))
            stderr = "" if claim_returncode == 0 else "claim failed"
            return backlog_raw_proc(args, claim_returncode, "", stderr)

        def fake_run_git(args, cwd=None):
            calls.append(("git", list(args)))
            if args[:1] == ["status"]:
                return git_proc(args, 0, status_stdout, "")
            if args[:1] == ["add"]:
                stderr = "" if add_returncode == 0 else "add failed"
                return git_proc(args, add_returncode, "", stderr)
            if args[:1] == ["commit"]:
                stderr = "" if commit_returncode == 0 else "commit failed"
                return git_proc(args, commit_returncode, "", stderr)
            return fake_git_inner(args, cwd=cwd)

        with mock.patch.object(server, "run_backlog_raw", side_effect=fake_run_backlog_raw), \
             mock.patch.object(server, "run_git", side_effect=fake_run_git), \
             mock.patch.object(server, "run_tmux", return_value=tmux_proc([], 0)), \
             mock.patch.object(server, "list_sessions", return_value=[]), \
             mock.patch("os.path.isdir", return_value=False), \
             mock.patch("os.makedirs"):
            result = spawn.spawn(self.config, "my-app", "TASK-2")
        return result, calls

    def test_claim_and_commit_precede_worktree_creation(self):
        result, calls = self._run(status_stdout=" M backlog/tasks/task-2.md\n")

        self.assertNotIn("warnings", result)

        claim_idx = next(i for i, c in enumerate(calls) if c[0] == "backlog_raw")
        commit_idx = next(i for i, c in enumerate(calls) if c[0] == "git" and c[1][:1] == ["commit"])
        add_idx = next(
            i for i, c in enumerate(calls) if c[0] == "git" and c[1][:2] == ["worktree", "add"]
        )
        self.assertLess(claim_idx, add_idx, "the claim must run before the worktree is cut")
        self.assertLess(commit_idx, add_idx, "the commit must run before the worktree is cut")

        self.assertEqual(
            calls[claim_idx],
            ("backlog_raw", ["task", "edit", "TASK-2", "-s", "In Progress"]),
        )
        commit_call = calls[commit_idx]
        self.assertEqual(commit_call[1][0], "commit")
        self.assertIn("-m", commit_call[1])
        self.assertEqual(commit_call[1][commit_call[1].index("-m") + 1], "backlog: claim TASK-2 for spawn")

    def test_nothing_to_commit_skips_add_and_commit_silently(self):
        result, calls = self._run(status_stdout="")  # nothing changed under backlog/

        self.assertNotIn("warnings", result)
        self.assertFalse(any(c[0] == "git" and c[1][:1] == ["add"] for c in calls))
        self.assertFalse(any(c[0] == "git" and c[1][:1] == ["commit"] for c in calls))
        # The status check itself is scoped to the backlog/ path only.
        status_call = next(c for c in calls if c[0] == "git" and c[1][:1] == ["status"])
        self.assertEqual(status_call[1], ["status", "--porcelain", "--", "backlog"])

    def test_claim_failure_becomes_warning_but_spawn_still_succeeds(self):
        result, _ = self._run(claim_returncode=1, status_stdout="")

        self.assertIn("warnings", result)
        self.assertEqual(len(result["warnings"]), 1)
        self.assertIn("TASK-2", result["warnings"][0])
        self.assertEqual(result["session"], "centrale-my-app-task-2")

    def test_add_failure_becomes_warning_but_spawn_still_succeeds(self):
        result, _ = self._run(status_stdout=" M backlog/tasks/task-2.md\n", add_returncode=1)

        self.assertIn("warnings", result)
        self.assertEqual(result["session"], "centrale-my-app-task-2")

    def test_commit_failure_becomes_warning_but_spawn_still_succeeds(self):
        result, _ = self._run(status_stdout=" M backlog/tasks/task-2.md\n", commit_returncode=1)

        self.assertIn("warnings", result)
        self.assertEqual(result["session"], "centrale-my-app-task-2")

    def test_claim_only_edits_status_and_leaves_assignee_alone(self):
        # No '-a'/'--assignee' flag should ever be passed: the existing
        # assignee must be left untouched.
        _, calls = self._run(status_stdout="")
        claim_args = next(c[1] for c in calls if c[0] == "backlog_raw")
        self.assertNotIn("-a", claim_args)
        self.assertNotIn("--assignee", claim_args)


class ResumeCmdForAgentTests(unittest.TestCase):
    """Unit tests for spawn.resume_cmd_for_agent, the small lookup
    resume() uses to find an agent's configured resumeCmd (looked up
    separately from resolve_agent so that function's tested 3-value
    return shape didn't need to change)."""

    def _config(self, raw_agents):
        return {"agents": server.normalize_agents_map(raw_agents)}

    def test_returns_configured_resume_cmd(self):
        config = self._config({"claude": {"cmd": ["claude"], "resumeCmd": ["claude", "--continue"]}})
        self.assertEqual(spawn.resume_cmd_for_agent(config, "claude"), ["claude", "--continue"])

    def test_returns_none_when_agent_has_no_resume_cmd(self):
        config = self._config({"claude": ["claude"]})
        self.assertIsNone(spawn.resume_cmd_for_agent(config, "claude"))

    def test_returns_none_for_unknown_agent_name(self):
        config = self._config({"claude": ["claude"]})
        self.assertIsNone(spawn.resume_cmd_for_agent(config, "ghost"))

    def test_returns_none_for_falsy_agent_name(self):
        config = self._config({"claude": ["claude"]})
        self.assertIsNone(spawn.resume_cmd_for_agent(config, None))

    def test_matches_case_insensitively_like_resolve_agent(self):
        config = self._config({"Claude": {"cmd": ["claude"], "resumeCmd": ["claude", "--continue"]}})
        self.assertEqual(spawn.resume_cmd_for_agent(config, "claude"), ["claude", "--continue"])


class ResumeIntegrationTests(unittest.TestCase):
    """Full spawn.resume() runs (no CENTRALE_SPAWN_CMD override), covering
    argv selection per agent form, the fresh-start fallback, and that it
    reuses spawn()'s own validation/naming/409/worktree machinery rather
    than re-implementing any of it."""

    def setUp(self):
        self.config = make_config(
            "/worktrees", [{"name": "my-app", "path": "/repos/my-app"}]
        )
        self.env_patch = mock.patch.dict(os.environ, {}, clear=False)
        self.env_patch.start()
        os.environ.pop("CENTRALE_SPAWN_CMD", None)
        self.addCleanup(self.env_patch.stop)

    def _run_resume(self, config, assignees, reconcile=False, sessions=None, status=None):
        fake_git = FakeGit(branch_exists=True, worktree_registered=True, wt_dir="/worktrees/my-app-task-2")
        with mock.patch.object(server, "run_git", side_effect=fake_git), \
             mock.patch.object(server, "run_tmux", return_value=tmux_proc([], 0)) as run_tmux, \
             mock.patch.object(server, "list_sessions", return_value=sessions or []), \
             mock.patch.object(server, "run_backlog", return_value=task_view(assignees, status=status)), \
             mock.patch.object(server, "run_backlog_raw") as run_backlog_raw, \
             mock.patch.object(server, "ensure_hooks_settings_file", return_value="/fake/cache/centrale/hooks-settings.json"), \
             mock.patch.object(server, "probe_codex_hook_trust", return_value=False), \
             mock.patch("os.path.isdir", return_value=True), \
             mock.patch("os.makedirs"):
            if reconcile:
                result = spawn.resume(config, "my-app", "TASK-2", reconcile=True)
            else:
                result = spawn.resume(config, "my-app", "TASK-2")
        return result, run_tmux, run_backlog_raw, fake_git

    def test_configured_resume_cmd_is_used_with_no_prompt_argument(self):
        config = make_config("/worktrees", [{"name": "my-app", "path": "/repos/my-app"}])
        config["agents"] = server.normalize_agents_map({
            "claude": {"cmd": ["claude"], "resumeCmd": ["claude", "--continue", "--yolo"]},
        })
        result, run_tmux, _, _ = self._run_resume(config, [])

        self.assertTrue(result["resumed"])
        tmux_args = new_session_argv(run_tmux)
        # The configured resumeCmd, then the claude hooks injection
        # (task-37) appended after it since there's no prompt argument to
        # stay in front of.
        self.assertEqual(tmux_args[-5:-2], ["claude", "--continue", "--yolo"])
        self.assertEqual(tmux_args[-2], "--settings")
        # No prompt argument at all -- the resumed conversation has its
        # own context already.
        self.assertNotEqual(tmux_args[-1], spawn.prompt_for("TASK-2"))

    def test_claude_family_agent_without_resume_cmd_defaults_to_continue(self):
        # "claude-sonnet": ["claude", "--model", "sonnet"] -- a
        # differently-named agent whose underlying command is still
        # literally `claude`, so it still counts as claude-family.
        config = make_config("/worktrees", [{"name": "my-app", "path": "/repos/my-app"}])
        config["agents"] = server.normalize_agents_map({
            "claude-sonnet": ["claude", "--model", "sonnet"],
        })
        config["defaultAgent"] = "claude-sonnet"
        result, run_tmux, _, _ = self._run_resume(config, [])

        self.assertEqual(result["agent"], "claude-sonnet")
        tmux_args = new_session_argv(run_tmux)
        self.assertEqual(tmux_args[-4:-2], ["claude", "--continue"])
        self.assertEqual(tmux_args[-2:], ["--settings", "/fake/cache/centrale/hooks-settings.json"])
        self.assertIn(
            f"CENTRALE_EVENT_URL={spawn.event_url(config, 'my-app', 'TASK-2', agent_kind='claude')}",
            tmux_args,
        )

    def test_agent_in_neither_family_without_resume_cmd_falls_back_to_fresh_start_with_note(self):
        # task-115: claude AND codex both have family resume defaults now,
        # so the fresh-start fallback needs an agent that is neither.
        config = make_config("/worktrees", [{"name": "my-app", "path": "/repos/my-app"}])
        config["agents"] = server.normalize_agents_map({"aider": ["aider"]})
        config["defaultAgent"] = "aider"
        result, run_tmux, _, _ = self._run_resume(config, [])

        self.assertEqual(result["agent"], "aider")
        tmux_args = new_session_argv(run_tmux)
        # The fresh-start fallback's own cmd, then the prompt last. No
        # hook/notify injection: that dispatches on the claude/codex
        # basenames only.
        self.assertEqual(tmux_args[-2], "aider")
        self.assertIn(
            "CENTRALE_EVENT_URL="
            + spawn.event_url(
                config, "my-app", "TASK-2",
                agent_kind=spawn.agent_kind_for_command(["aider"]),
            ),
            tmux_args,
        )
        prompt_arg = tmux_args[-1]
        self.assertTrue(prompt_arg.startswith(spawn.prompt_for("TASK-2")))
        self.assertIn("uncommitted", prompt_arg.lower())
        self.assertIn("prior work", prompt_arg.lower())
        # Task-36: the resume fresh-start fallback must carry the
        # no-self-merge boundary too -- inherited automatically here
        # since it builds on prompt_for()'s own output (see
        # RESUME_FALLBACK_NOTE's comment), not duplicated separately.
        self.assertIn("Do NOT merge this branch", prompt_arg)

    def test_fallback_honors_the_spawn_prompt_override(self):
        # Task-69: the fresh-start fallback builds on prompt_for(), so a
        # configured spawnPrompt reaches a resumed non-claude agent too.
        config = make_config("/worktrees", [{"name": "my-app", "path": "/repos/my-app"}])
        config["spawnPrompt"] = "Resume-capable custom prompt for {task_id}."
        config["agents"] = server.normalize_agents_map({"aider": ["aider"]})
        config["defaultAgent"] = "aider"
        _, run_tmux, _, _ = self._run_resume(config, [])
        tmux_args = new_session_argv(run_tmux)
        self.assertTrue(tmux_args[-1].startswith("Resume-capable custom prompt for TASK-2."))
        self.assertIn("prior work", tmux_args[-1].lower())
        self.assertNotIn("Backlog.md workflow", tmux_args[-1])

    def test_fallback_appends_prompt_suffix_after_the_resume_note(self):
        config = make_config("/worktrees", [{"name": "my-app", "path": "/repos/my-app"}])
        config["agents"] = server.normalize_agents_map({
            "aider": {"cmd": ["aider"], "promptSuffix": "Prefer small commits."},
        })
        config["defaultAgent"] = "aider"
        _, run_tmux, _, _ = self._run_resume(config, [])
        tmux_args = new_session_argv(run_tmux)
        self.assertTrue(tmux_args[-1].endswith("Prefer small commits."))

    def test_codex_family_agent_without_resume_cmd_defaults_to_resume_last(self):
        # task-115: the codex equivalent of `claude --continue`. Both the
        # picker and --last filter by working directory unless --all is
        # passed, and every task has its own worktree, so this continues
        # THIS task's conversation rather than some other repo's.
        config = make_config("/worktrees", [{"name": "my-app", "path": "/repos/my-app"}])
        config["agents"] = server.normalize_agents_map({"codex": ["codex"]})
        config["defaultAgent"] = "codex"
        result, run_tmux, _, _ = self._run_resume(config, [])

        self.assertEqual(result["agent"], "codex")
        tmux_args = new_session_argv(run_tmux)
        cmd_start = tmux_args.index("codex")
        self.assertEqual(tmux_args[cmd_start:cmd_start + 3], ["codex", "resume", "--last"])
        # No prompt: the resumed conversation carries its own context.
        self.assertNotIn(spawn.prompt_for("TASK-2"), tmux_args)
        # The codex notify/hook injection still rides along -- `codex
        # resume` accepts -c exactly as the top-level command does.
        self.assertIn("-c", tmux_args[cmd_start + 3:])
        self.assertIn(
            f"CENTRALE_EVENT_URL={spawn.event_url(config, 'my-app', 'TASK-2', agent_kind='codex')}",
            tmux_args,
        )

    def test_resume_cmd_takes_priority_over_codex_default(self):
        # Tier 1 still wins: a configured resumeCmd is the user's own
        # choice and must beat the family default.
        config = make_config("/worktrees", [{"name": "my-app", "path": "/repos/my-app"}])
        config["agents"] = server.normalize_agents_map({
            "codex": {"cmd": ["codex"], "resumeCmd": ["codex", "resume", "--yolo"]},
        })
        config["defaultAgent"] = "codex"
        _, run_tmux, _, _ = self._run_resume(config, [])
        tmux_args = new_session_argv(run_tmux)
        cmd_start = tmux_args.index("codex")
        self.assertEqual(tmux_args[cmd_start:cmd_start + 3], ["codex", "resume", "--yolo"])

    def test_resume_cmd_takes_priority_over_claude_default(self):
        config = make_config("/worktrees", [{"name": "my-app", "path": "/repos/my-app"}])
        config["agents"] = server.normalize_agents_map({
            "claude": {"cmd": ["claude"], "resumeCmd": ["claude", "--resume", "last"]},
        })
        _, run_tmux, _, _ = self._run_resume(config, [])
        tmux_args = new_session_argv(run_tmux)
        self.assertEqual(tmux_args[-5:-2], ["claude", "--resume", "last"])
        self.assertEqual(tmux_args[-2], "--settings")

    def test_normal_resume_result_has_no_reconcile_marker(self):
        # task-66 AC #3: the plain resume path is byte-for-byte what it
        # was -- no reconcile key, `claude --continue`, no prompt.
        config = make_config("/worktrees", [{"name": "my-app", "path": "/repos/my-app"}])
        result, run_tmux, _, _ = self._run_resume(config, [])
        self.assertNotIn("reconcile", result)
        tmux_args = new_session_argv(run_tmux)
        self.assertEqual(tmux_args[-4:-2], ["claude", "--continue"])
        self.assertEqual(tmux_args[-2], "--settings")

    def test_resume_never_reclaims_or_recommits_the_task(self):
        # Unlike spawn(), resume() must not touch backlog/'s claim
        # workflow -- the task was already claimed the first time it was
        # spawned; resume only continues work already underway.
        config = make_config("/worktrees", [{"name": "my-app", "path": "/repos/my-app"}])
        _, _, run_backlog_raw, _ = self._run_resume(config, [])
        run_backlog_raw.assert_not_called()

    def test_resume_reuses_existing_worktree_without_creating_a_new_branch(self):
        config = make_config("/worktrees", [{"name": "my-app", "path": "/repos/my-app"}])
        _, _, _, fake_git = self._run_resume(config, [])
        self.assertFalse(any(c[:2] == ["worktree", "add"] for c in fake_git.calls))

    def test_agent_name_and_session_come_back_in_result(self):
        config = make_config("/worktrees", [{"name": "my-app", "path": "/repos/my-app"}])
        result, _, _, _ = self._run_resume(config, [])
        self.assertEqual(result["session"], "centrale-my-app-task-2")
        self.assertEqual(result["attach"], "tmux attach -t centrale-my-app-task-2")
        self.assertEqual(result["agent"], "claude")
        self.assertTrue(result["resumed"])

    def test_spawn_cmd_override_wins_and_skips_agent_resolution(self):
        with mock.patch.dict(os.environ, {"CENTRALE_SPAWN_CMD": "sleep 300"}):
            fake_git = FakeGit(branch_exists=True, worktree_registered=True, wt_dir="/worktrees/my-app-task-2")
            with mock.patch.object(server, "run_git", side_effect=fake_git), \
                 mock.patch.object(server, "run_tmux", return_value=tmux_proc([], 0)) as run_tmux, \
                 mock.patch.object(server, "list_sessions", return_value=[]), \
                 mock.patch.object(server, "run_backlog") as run_backlog, \
                 mock.patch.object(server, "run_backlog_raw") as run_backlog_raw, \
                 mock.patch("os.path.isdir", return_value=True), \
                 mock.patch("os.makedirs"):
                result = spawn.resume(self.config, "my-app", "TASK-2")

        run_backlog.assert_not_called()
        run_backlog_raw.assert_not_called()
        self.assertEqual(result["agent"], "custom")
        tmux_args = new_session_argv(run_tmux)
        # Same as spawn()'s override case: no CENTRALE_AGENT and no argv
        # hook injection, but CENTRALE_EVENT_URL is still set (AC #4/#6).
        self.assertEqual(tmux_args.count("-e"), 1)
        self.assertNotIn("CENTRALE_AGENT=", "".join(tmux_args))
        self.assertEqual(
            tmux_args,
            ["new-session", "-d", "-s", "centrale-my-app-task-2", "-c", "/worktrees/my-app-task-2",
             *geometry_args(),
             "-e", f"CENTRALE_EVENT_URL={spawn.event_url(self.config, 'my-app', 'TASK-2')}",
             "sleep", "300", spawn.prompt_for("TASK-2")],
        )


class ReconcileResumeTests(ResumeIntegrationTests):
    """task-66: resume(..., reconcile=True) is the same resume path with
    a different job for the agent; task-133: delivered through the same
    three tiers plain Resume uses (resumeCmd, `claude --continue` /
    `codex resume --last`, fresh start), with the reconcile prompt as the
    trailing argument at each. Inherits ResumeIntegrationTests'
    harness (and, deliberately, every one of its normal-resume tests --
    they must still pass untouched with this subclass's setUp)."""

    def _project(self, **extra):
        return {"name": "my-app", "path": "/repos/my-app", **extra}

    def _assert_reconcile_prompt_content(self, prompt_arg, resumed):
        """The job itself is the same at every tier: orient, merge base in
        (never rebase), fix textual and semantic conflicts, run the check
        command, commit here, never merge to base."""
        self.assertIn("Reconcile backlog task TASK-2", prompt_arg)
        self.assertIn("`backlog task view TASK-2 --plain`", prompt_arg)
        self.assertIn("`git diff main...HEAD`", prompt_arg)
        self.assertLess(prompt_arg.index("orient"), prompt_arg.index("`git merge main`"))
        self.assertIn("`git merge main`", prompt_arg)
        self.assertIn("do NOT rebase", prompt_arg)
        self.assertIn("textual and semantic", prompt_arg)
        self.assertIn("Do NOT merge this branch into `main`", prompt_arg)
        self.assertIn("backlog instructions overview", prompt_arg)
        # task-133: a continued conversation is told it built this branch
        # and that its memory of the base is stale; a fresh start is not
        # (it never saw the branch being built).
        if resumed:
            self.assertIn("You built this branch in this conversation", prompt_arg)
            self.assertIn("what you remember of `main` is stale", prompt_arg)
        else:
            self.assertNotIn("You built this branch", prompt_arg)

    def test_reconcile_uses_the_configured_resume_cmd_with_the_prompt_appended(self):
        # task-133 AC #3 (tier 1): the user's own resumeCmd wins, exactly
        # as on a plain Resume, and the reconcile prompt is the ONE
        # trailing argument -- before task-133 this ran the agent's plain
        # cmd in a fresh conversation instead.
        config = make_config("/worktrees", [self._project(checkCommand="python3 -m unittest discover tests")])
        config["agents"] = server.normalize_agents_map({
            "claude": {"cmd": ["claude"], "resumeCmd": ["claude", "--continue", "--yolo"]},
        })
        result, run_tmux, _, _ = self._run_resume(config, [], reconcile=True)

        self.assertEqual(result["agent"], "claude")
        self.assertTrue(result["resumed"])
        self.assertTrue(result["reconcile"])
        tmux_args = new_session_argv(run_tmux)
        # resumeCmd verbatim, then the same hook injection as any
        # claude-family spawn/resume, then the prompt last.
        self.assertEqual(tmux_args[-6:-3], ["claude", "--continue", "--yolo"])
        self.assertEqual(tmux_args[-3], "--settings")
        prompt_arg = tmux_args[-1]
        self.assertEqual(
            prompt_arg,
            spawn.reconcile_prompt_for(
                "TASK-2", "main", "python3 -m unittest discover tests", resumed=True
            ),
        )
        self.assertIn("`python3 -m unittest discover tests`", prompt_arg)
        self._assert_reconcile_prompt_content(prompt_arg, resumed=True)
        # Identical session naming and lifecycle-event plumbing.
        self.assertEqual(tmux_args[:5], ["new-session", "-d", "-s", "centrale-my-app-task-2", "-c"])
        self.assertIn("CENTRALE_AGENT=claude", tmux_args)
        self.assertIn(
            f"CENTRALE_EVENT_URL={spawn.event_url(config, 'my-app', 'TASK-2', agent_kind='claude')}",
            tmux_args,
        )

    def test_reconcile_claude_family_without_resume_cmd_continues_with_the_prompt(self):
        # task-133 AC #1 (tier 2, claude): `claude --continue <prompt>` in
        # the task worktree -- the conversation that built the branch,
        # handed the new job.
        config = make_config("/worktrees", [self._project(checkCommand="make check")])
        result, run_tmux, _, _ = self._run_resume(config, [], reconcile=True)

        self.assertEqual(result["agent"], "claude")
        self.assertTrue(result["reconcile"])
        tmux_args = new_session_argv(run_tmux)
        self.assertEqual(tmux_args[-5:-3], ["claude", "--continue"])
        self.assertEqual(tmux_args[-3], "--settings")
        self.assertEqual(
            tmux_args[-1],
            spawn.reconcile_prompt_for("TASK-2", "main", "make check", resumed=True),
        )
        self._assert_reconcile_prompt_content(tmux_args[-1], resumed=True)
        # The worktree is the session's cwd, which is how --continue finds
        # this task's own conversation rather than some other repo's.
        self.assertEqual(tmux_args[4:6], ["-c", "/worktrees/my-app-task-2"])

    def test_reconcile_codex_family_without_resume_cmd_resumes_last_with_the_prompt(self):
        # task-133 AC #2 (tier 2, codex): `codex resume --last <prompt>`
        # ("[PROMPT] Optional user prompt to start the session"), with the
        # codex notify injection still riding along in between.
        config = make_config("/worktrees", [self._project()])
        config["agents"] = server.normalize_agents_map({"codex": ["codex"]})
        config["defaultAgent"] = "codex"
        result, run_tmux, _, _ = self._run_resume(config, [], reconcile=True)

        self.assertEqual(result["agent"], "codex")
        tmux_args = new_session_argv(run_tmux)
        cmd_start = tmux_args.index("codex")
        self.assertEqual(tmux_args[cmd_start:cmd_start + 3], ["codex", "resume", "--last"])
        self.assertEqual(tmux_args[cmd_start + 3], "-c")
        self.assertEqual(
            tmux_args[-1], spawn.reconcile_prompt_for("TASK-2", "main", resumed=True)
        )
        self._assert_reconcile_prompt_content(tmux_args[-1], resumed=True)

    def test_reconcile_agent_in_neither_family_falls_back_to_fresh_start_with_note(self):
        # task-133 AC #4 (tier 3): nothing to resume, so exactly the old
        # behaviour -- the agent's own cmd with the (fresh-framed)
        # reconcile prompt -- plus the same prior-work note a plain Resume
        # appends on its fresh start, so the agent hears a fresh start
        # happened.
        config = make_config("/worktrees", [self._project(checkCommand="make check")])
        config["agents"] = server.normalize_agents_map({"aider": ["aider"]})
        config["defaultAgent"] = "aider"
        result, run_tmux, _, _ = self._run_resume(config, [], reconcile=True)

        self.assertEqual(result["agent"], "aider")
        self.assertTrue(result["reconcile"])
        tmux_args = new_session_argv(run_tmux)
        self.assertEqual(tmux_args[-2], "aider")
        self.assertNotIn("--continue", tmux_args)
        prompt_arg = tmux_args[-1]
        fresh = spawn.reconcile_prompt_for("TASK-2", "main", "make check")
        self.assertEqual(prompt_arg, fresh + spawn.RESUME_FALLBACK_NOTE)
        self.assertIn("prior work", prompt_arg.lower())
        self._assert_reconcile_prompt_content(prompt_arg, resumed=False)

    def test_reconcile_appends_prompt_suffix_last_at_every_tier(self):
        # Tier 2 (codex): suffix after the prompt.
        config = make_config("/worktrees", [self._project()])
        config["agents"] = server.normalize_agents_map({
            "codex": {"cmd": ["codex"], "promptSuffix": "Prefer small commits."},
        })
        config["defaultAgent"] = "codex"
        result, run_tmux, _, _ = self._run_resume(config, [], reconcile=True)
        self.assertEqual(result["agent"], "codex")
        tmux_args = new_session_argv(run_tmux)
        self.assertEqual(tmux_args[-6:-3], ["codex", "resume", "--last"])
        self.assertEqual(tmux_args[-3], "-c")  # codex notify injection, as on every spawn/resume
        prompt_arg = tmux_args[-1]
        self.assertTrue(prompt_arg.startswith("Reconcile backlog task TASK-2"))
        self.assertTrue(prompt_arg.endswith("\n\nPrefer small commits."))

        # Tier 1 (resumeCmd): same placement.
        config["agents"] = server.normalize_agents_map({
            "codex": {"cmd": ["codex"], "resumeCmd": ["codex", "resume", "--yolo"],
                      "promptSuffix": "Prefer small commits."},
        })
        _, run_tmux, _, _ = self._run_resume(config, [], reconcile=True)
        tmux_args = new_session_argv(run_tmux)
        self.assertEqual(tmux_args[-6:-3], ["codex", "resume", "--yolo"])
        self.assertTrue(tmux_args[-1].endswith("\n\nPrefer small commits."))

        # Tier 3 (fresh start): prompt, then the note, then the suffix --
        # the same order a plain Resume's fresh start uses.
        config["agents"] = server.normalize_agents_map({
            "aider": {"cmd": ["aider"], "promptSuffix": "Prefer small commits."},
        })
        config["defaultAgent"] = "aider"
        _, run_tmux, _, _ = self._run_resume(config, [], reconcile=True)
        tmux_args = new_session_argv(run_tmux)
        self.assertEqual(tmux_args[-2], "aider")
        self.assertEqual(
            tmux_args[-1],
            spawn.reconcile_prompt_for("TASK-2", "main") + spawn.RESUME_FALLBACK_NOTE
            + "\n\nPrefer small commits.",
        )

    def test_resumed_reconcile_prompt_differs_from_fresh_by_one_framing_sentence_only(self):
        # task-133: the no-self-merge boundary and the check-command
        # instruction are byte-for-byte what they were; the resumed
        # variant only adds RECONCILE_RESUMED_CONTEXT ahead of the
        # orientation step.
        fresh = spawn.reconcile_prompt_for("TASK-2", "main", "make check")
        resumed = spawn.reconcile_prompt_for("TASK-2", "main", "make check", resumed=True)
        context = spawn.RECONCILE_RESUMED_CONTEXT.format(base_branch="main")
        self.assertIn(context, resumed)
        self.assertEqual(resumed.replace(context, "", 1), fresh)
        self.assertLess(resumed.index(context), resumed.index("Run `backlog instructions overview`"))
        self.assertIn("read the repository, not your memory of it", resumed)
        self.assertNotIn("You built this branch", fresh)

    def test_reconcile_prompt_uses_the_repos_current_base_branch(self):
        config = make_config("/worktrees", [self._project()])
        fake_git = FakeGit(branch_exists=True, worktree_registered=True,
                           wt_dir="/worktrees/my-app-task-2", current_branch="develop")
        with mock.patch.object(server, "run_git", side_effect=fake_git), \
             mock.patch.object(server, "run_tmux", return_value=tmux_proc([], 0)) as run_tmux, \
             mock.patch.object(server, "list_sessions", return_value=[]), \
             mock.patch.object(server, "run_backlog", return_value=task_view([])), \
             mock.patch.object(server, "run_backlog_raw"), \
             mock.patch.object(server, "ensure_hooks_settings_file", return_value="/fake/hooks.json"), \
             mock.patch("os.path.isdir", return_value=True), \
             mock.patch("os.makedirs"):
            spawn.resume(config, "my-app", "TASK-2", reconcile=True)
        tmux_args = new_session_argv(run_tmux)
        self.assertIn("`git merge develop`", tmux_args[-1])
        self.assertNotIn("main", tmux_args[-1])

    def test_reconcile_prompt_without_check_command_points_at_project_docs(self):
        config = make_config("/worktrees", [self._project()])
        _, run_tmux, _, _ = self._run_resume(config, [], reconcile=True)
        tmux_args = new_session_argv(run_tmux)
        self.assertIn("see the project's AGENTS.md/README", tmux_args[-1])

    def test_reconcile_never_merges_rebases_reclaims_or_recreates_anything(self):
        # AC #2: Centrale itself performs NO merge/rebase -- the only git
        # it runs is the same worktree-reuse lookup a plain resume does.
        config = make_config("/worktrees", [self._project()])
        _, _, run_backlog_raw, fake_git = self._run_resume(config, [], reconcile=True)
        run_backlog_raw.assert_not_called()
        subcommands = [c[0] for c in fake_git.calls]
        self.assertNotIn("merge", subcommands)
        self.assertNotIn("rebase", subcommands)
        self.assertFalse(any(c[:2] == ["worktree", "add"] for c in fake_git.calls))

    def test_reconcile_gets_the_same_409_on_a_live_session(self):
        config = make_config("/worktrees", [self._project()])
        live = [{"name": "centrale-my-app-task-2", "created": "0", "attached": False}]
        with self.assertRaises(spawn.SpawnError) as ctx:
            self._run_resume(config, [], reconcile=True, sessions=live)
        self.assertEqual(ctx.exception.status, 409)

    def test_reconcile_refuses_a_done_task_like_any_resume(self):
        config = make_config("/worktrees", [self._project()])
        with self.assertRaises(spawn.SpawnError) as ctx:
            self._run_resume(config, [], reconcile=True, status="Done")
        self.assertEqual(ctx.exception.status, 409)

    def test_reconcile_under_spawn_cmd_override_still_uses_reconcile_prompt(self):
        config = make_config("/worktrees", [self._project(checkCommand="make check")])
        fake_git = FakeGit(branch_exists=True, worktree_registered=True, wt_dir="/worktrees/my-app-task-2")
        with mock.patch.dict(os.environ, {"CENTRALE_SPAWN_CMD": "true"}), \
             mock.patch.object(server, "run_git", side_effect=fake_git), \
             mock.patch.object(server, "run_tmux", return_value=tmux_proc([], 0)) as run_tmux, \
             mock.patch.object(server, "list_sessions", return_value=[]), \
             mock.patch.object(server, "run_backlog") as run_backlog, \
             mock.patch("os.path.isdir", return_value=True), \
             mock.patch("os.makedirs"):
            result = spawn.resume(config, "my-app", "TASK-2", reconcile=True)
        run_backlog.assert_not_called()
        self.assertEqual(result["agent"], "custom")
        self.assertTrue(result["reconcile"])
        tmux_args = new_session_argv(run_tmux)
        # A fresh process by construction, so the fresh framing (no "you
        # built this branch") and, as for a plain resume under the
        # override, no fallback note either.
        self.assertEqual(tmux_args[-2:], ["true", spawn.reconcile_prompt_for("TASK-2", "main", "make check")])
        self.assertNotIn("You built this branch", tmux_args[-1])


class ResumeValidationTests(unittest.TestCase):
    """Resume shares spawn()'s validation/409 behavior exactly (see
    spawn._validate_and_check_session) -- these mirror SpawnValidationTests."""

    def setUp(self):
        self.config = make_config(
            "/worktrees", [{"name": "my-app", "path": "/repos/my-app"}]
        )

    def test_tmux_unavailable_rejected_before_any_subprocess_call(self):
        config = make_config("/worktrees", [{"name": "my-app", "path": "/repos/my-app"}])
        config["capabilities"] = {"tmux": False}
        with mock.patch.object(server, "run_git") as run_git, \
             mock.patch.object(server, "run_tmux") as run_tmux, \
             mock.patch.object(server, "list_sessions") as list_sessions:
            with self.assertRaises(spawn.SpawnError) as ctx:
                spawn.resume(config, "my-app", "TASK-2")
            self.assertEqual(ctx.exception.status, 503)
            run_git.assert_not_called()
            run_tmux.assert_not_called()
            list_sessions.assert_not_called()

    def test_unknown_project_rejected(self):
        with mock.patch.object(server, "run_git") as run_git, \
             mock.patch.object(server, "run_tmux") as run_tmux:
            with self.assertRaises(spawn.SpawnError) as ctx:
                spawn.resume(self.config, "nonexistent", "TASK-2")
            self.assertEqual(ctx.exception.status, 404)
            run_git.assert_not_called()
            run_tmux.assert_not_called()

    def test_malformed_task_id_rejected(self):
        with mock.patch.object(server, "run_git") as run_git, \
             mock.patch.object(server, "run_tmux") as run_tmux:
            with self.assertRaises(spawn.SpawnError) as ctx:
                spawn.resume(self.config, "my-app", "not a task id")
            self.assertEqual(ctx.exception.status, 400)
            run_git.assert_not_called()
            run_tmux.assert_not_called()

    def test_duplicate_session_returns_409_same_name_as_spawn_would_use(self):
        with mock.patch.object(server, "run_git") as run_git, \
             mock.patch.object(server, "run_tmux") as run_tmux, \
             mock.patch.object(
                 server, "list_sessions",
                 return_value=[{"name": "centrale-my-app-task-2", "created": "0", "attached": False}],
             ):
            with self.assertRaises(spawn.SpawnError) as ctx:
                spawn.resume(self.config, "my-app", "TASK-2")
            self.assertEqual(ctx.exception.status, 409)
            self.assertIn("centrale-my-app-task-2", str(ctx.exception))
            run_git.assert_not_called()
            run_tmux.assert_not_called()


class DoneStatusRefusalTests(unittest.TestCase):
    """Task-41: POST /api/spawn (and resume) must refuse a task whose
    status is terminal "Done" with a 409, before any claim commit,
    worktree creation, or tmux session -- spawning a Done task must never
    claim it back to "In Progress" (committed to main!) or launch an
    agent onto already-finished work. Skipped entirely under the
    CENTRALE_SPAWN_CMD hermetic test override (see spawn._check_not_done),
    like resolve_agent's own backlog CLI call, so the rest of the test
    suite stays free to spawn probes onto arbitrary/fixture task ids.
    """

    def setUp(self):
        self.config = make_config(
            "/worktrees", [{"name": "my-app", "path": "/repos/my-app"}]
        )
        # Make sure no leftover CENTRALE_SPAWN_CMD from another test
        # process forces the override (and its Done-check skip) here.
        self.env_patch = mock.patch.dict(os.environ, {}, clear=False)
        self.env_patch.start()
        os.environ.pop("CENTRALE_SPAWN_CMD", None)
        self.addCleanup(self.env_patch.stop)

    def _mocks(self, status, branch_exists=True, worktree_registered=True):
        fake_git = FakeGit(
            branch_exists=branch_exists, worktree_registered=worktree_registered,
            wt_dir="/worktrees/my-app-task-2",
        )
        return dict(
            run_git=mock.patch.object(server, "run_git", side_effect=fake_git),
            run_tmux=mock.patch.object(server, "run_tmux", return_value=tmux_proc([], 0)),
            list_sessions=mock.patch.object(server, "list_sessions", return_value=[]),
            run_backlog=mock.patch.object(server, "run_backlog", return_value=task_view([], status=status)),
            run_backlog_raw=mock.patch.object(server, "run_backlog_raw", return_value=backlog_raw_proc()),
            isdir=mock.patch("os.path.isdir", return_value=True),
            makedirs=mock.patch("os.makedirs"),
        ), fake_git

    def test_spawn_refuses_done_task_before_any_side_effect(self):
        mocks, fake_git = self._mocks(status="Done")
        with mocks["run_git"], mocks["run_tmux"] as run_tmux, mocks["list_sessions"], \
             mocks["run_backlog"], mocks["run_backlog_raw"] as run_backlog_raw:
            with self.assertRaises(spawn.SpawnError) as ctx:
                spawn.spawn(self.config, "my-app", "TASK-2")

        self.assertEqual(ctx.exception.status, 409)
        self.assertIn("TASK-2", str(ctx.exception))
        self.assertIn("Done", str(ctx.exception))
        # No claim commit and no worktree/tmux side effect of any kind.
        run_backlog_raw.assert_not_called()
        self.assertEqual(fake_git.calls, [])
        run_tmux.assert_not_called()

    def test_resume_refuses_done_task_before_touching_the_worktree(self):
        mocks, fake_git = self._mocks(status="Done")
        with mocks["run_git"], mocks["run_tmux"] as run_tmux, mocks["list_sessions"], \
             mocks["run_backlog"], mocks["run_backlog_raw"] as run_backlog_raw:
            with self.assertRaises(spawn.SpawnError) as ctx:
                spawn.resume(self.config, "my-app", "TASK-2")

        self.assertEqual(ctx.exception.status, 409)
        self.assertIn("TASK-2", str(ctx.exception))
        self.assertIn("Done", str(ctx.exception))
        run_backlog_raw.assert_not_called()
        self.assertEqual(fake_git.calls, [])
        run_tmux.assert_not_called()

    def test_refusal_is_case_insensitive(self):
        for status in ("done", "DONE", "DoNe"):
            with self.subTest(status=status):
                mocks, _ = self._mocks(status=status)
                with mocks["run_git"], mocks["run_tmux"], mocks["list_sessions"], \
                     mocks["run_backlog"], mocks["run_backlog_raw"]:
                    with self.assertRaises(spawn.SpawnError) as ctx:
                        spawn.spawn(self.config, "my-app", "TASK-2")
                self.assertEqual(ctx.exception.status, 409)

    def test_in_progress_task_still_spawns(self):
        # AC #3: fresh-data behavior for a non-terminal status (the
        # interrupted/re-spawn path) must be unchanged.
        mocks, fake_git = self._mocks(status="In Progress")
        with mocks["run_git"], mocks["run_tmux"] as run_tmux, mocks["list_sessions"], \
             mocks["run_backlog"], mocks["run_backlog_raw"], mocks["isdir"], mocks["makedirs"]:
            result = spawn.spawn(self.config, "my-app", "TASK-2")
        self.assertEqual(result["session"], "centrale-my-app-task-2")
        new_session_argv(run_tmux)  # exactly one session launched

    def test_task_with_no_status_field_still_spawns(self):
        # A `backlog task view --json` response missing "status" entirely
        # (or the CLI call failing) must not be misread as Done.
        mocks, fake_git = self._mocks(status=None)
        with mocks["run_git"], mocks["run_tmux"] as run_tmux, mocks["list_sessions"], \
             mocks["run_backlog"], mocks["run_backlog_raw"], mocks["isdir"], mocks["makedirs"]:
            result = spawn.spawn(self.config, "my-app", "TASK-2")
        self.assertEqual(result["session"], "centrale-my-app-task-2")
        new_session_argv(run_tmux)  # exactly one session launched

    def test_spawn_cmd_override_skips_the_done_check(self):
        # The hermetic test override must keep working for probes spawned
        # onto arbitrary/fixture task ids, regardless of a real task's
        # actual backlog status -- run_backlog is never even called.
        with mock.patch.dict(os.environ, {"CENTRALE_SPAWN_CMD": "sleep 300"}):
            fake_git = FakeGit(branch_exists=True, worktree_registered=True, wt_dir="/worktrees/my-app-task-2")
            with mock.patch.object(server, "run_git", side_effect=fake_git), \
                 mock.patch.object(server, "run_tmux", return_value=tmux_proc([], 0)) as run_tmux, \
                 mock.patch.object(server, "list_sessions", return_value=[]), \
                 mock.patch.object(server, "run_backlog") as run_backlog, \
                 mock.patch.object(server, "run_backlog_raw", return_value=backlog_raw_proc()), \
                 mock.patch("os.path.isdir", return_value=True), \
                 mock.patch("os.makedirs"):
                result = spawn.spawn(self.config, "my-app", "TASK-2")
        run_backlog.assert_not_called()
        self.assertEqual(result["session"], "centrale-my-app-task-2")
        new_session_argv(run_tmux)  # exactly one session launched

    def test_status_check_reuses_the_single_task_view_fetch_for_agent_resolution(self):
        # task-41's refactor: the Done-status check and resolve_agent's
        # assignee lookup must share one `backlog task view` call, not
        # make two, for a real (non-override) spawn.
        mocks, fake_git = self._mocks(status="In Progress")
        with mocks["run_git"], mocks["run_tmux"], mocks["list_sessions"], \
             mocks["run_backlog"] as run_backlog, mocks["run_backlog_raw"], \
             mocks["isdir"], mocks["makedirs"]:
            spawn.spawn(self.config, "my-app", "TASK-2")
        run_backlog.assert_called_once_with(["task", "view", "TASK-2", "--json"], cwd="/repos/my-app")


FOREIGN_WT = "/repos/my-app/.worktrees/task-2-integrated"


def worktree_list_porcelain(entries):
    """`git worktree list --porcelain` output for [(path, branch_or_None)]:
    a None branch renders as a detached-HEAD block."""
    blocks = []
    for path, branch in entries:
        lines = [f"worktree {path}", "HEAD 0123456789abcdef0123456789abcdef01234567"]
        lines.append(f"branch refs/heads/{branch}" if branch else "detached")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks) + "\n"


class ExternalCheckoutTests(unittest.TestCase):
    """Task-70: a task/<id> branch adopted into a worktree Centrale
    doesn't manage (.worktrees/, /tmp, ...) is detected purely from
    `git worktree list --porcelain`, classified centrale / external /
    none, and refused by spawn()/resume() with a 409 naming the foreign
    checkout -- BEFORE the claim commit, where today's raw `git worktree
    add` error would only surface after main had already been touched."""

    def setUp(self):
        self.config = make_config("/worktrees", [{"name": "my-app", "path": "/repos/my-app"}])
        self.project = {"name": "my-app", "path": "/repos/my-app"}
        self.env_patch = mock.patch.dict(os.environ, {}, clear=False)
        self.env_patch.start()
        os.environ.pop("CENTRALE_SPAWN_CMD", None)
        os.environ.pop("CENTRALE_SPAWN_CMD", None)
        self.addCleanup(self.env_patch.stop)

    def test_parse_worktree_checkouts_maps_branches_skipping_detached_and_bare(self):
        porcelain = (
            "worktree /repos/my-app\nHEAD aaa\nbranch refs/heads/main\n\n"
            "worktree /repos/my-app/.worktrees/scratch\nHEAD bbb\ndetached\n\n"
            "worktree /repos/my-app.git\nbare\n\n"
            f"worktree {FOREIGN_WT}\nHEAD ccc\nbranch refs/heads/task/task-2\n\n"
            "worktree /worktrees/my-app-task-3\nHEAD ddd\nbranch refs/heads/task/task-3\n"
        )
        self.assertEqual(spawn.parse_worktree_checkouts(porcelain), {
            "main": "/repos/my-app",
            "task/task-2": FOREIGN_WT,
            "task/task-3": "/worktrees/my-app-task-3",
        })
        self.assertEqual(spawn.parse_worktree_checkouts(""), {})
        self.assertEqual(spawn.parse_worktree_checkouts(None), {})

    def test_branch_checkouts_is_empty_on_git_failure(self):
        with mock.patch.object(server, "run_git", return_value=git_proc([], 128, "", "fatal")) as run_git:
            self.assertEqual(spawn.branch_checkouts("/repos/my-app"), {})
        run_git.assert_called_once_with(["worktree", "list", "--porcelain"], cwd="/repos/my-app")

    def _git(self, entries, commit_ts="1700000000"):
        def fake_run_git(args, cwd=None):
            if args[:2] == ["worktree", "list"]:
                return git_proc(args, 0, worktree_list_porcelain(entries), "")
            if args[:1] == ["log"]:
                return git_proc(args, 0, commit_ts + "\n", "")
            return git_proc(args, 0, "", "")
        return fake_run_git

    def test_checkout_state_none_when_branch_checked_out_nowhere(self):
        with mock.patch.object(server, "run_git", side_effect=self._git([("/repos/my-app", "main")])):
            state = spawn.checkout_state(self.config, self.project, "TASK-2")
        self.assertEqual(state, {"kind": "none", "path": None})

    def test_checkout_state_centrale_for_the_managed_worktree_path(self):
        entries = [("/repos/my-app", "main"), ("/worktrees/my-app-task-2", "task/task-2")]
        with mock.patch.object(server, "run_git", side_effect=self._git(entries)) as run_git:
            state = spawn.checkout_state(self.config, self.project, "TASK-2")
        self.assertEqual(state, {"kind": "centrale", "path": "/worktrees/my-app-task-2"})
        # No last-commit lookup for the ordinary case -- one git call only.
        self.assertEqual([c.args[0][:2] for c in run_git.call_args_list], [["worktree", "list"]])

    def test_checkout_state_external_names_path_and_last_commit_age(self):
        entries = [("/repos/my-app", "main"), (FOREIGN_WT, "task/task-2")]
        with mock.patch.object(server, "run_git", side_effect=self._git(entries)) as run_git, \
             mock.patch.object(spawn, "_now", return_value=1700003600):
            state = spawn.checkout_state(self.config, self.project, "TASK-2")
        self.assertEqual(state, {
            "kind": "external",
            "path": FOREIGN_WT,
            "lastCommitAt": "2023-11-14T22:13:20Z",
            "lastCommitAgeSeconds": 3600,
        })
        log_call = [c for c in run_git.call_args_list if c.args[0][:1] == ["log"]]
        self.assertEqual(len(log_call), 1)
        self.assertEqual(log_call[0].args[0], ["log", "-1", "--format=%ct", "task/task-2"])
        self.assertEqual(log_call[0].kwargs.get("cwd"), "/repos/my-app")

    def test_checkout_state_external_tolerates_unreadable_commit_time(self):
        entries = [(FOREIGN_WT, "task/task-2")]
        with mock.patch.object(server, "run_git", side_effect=self._git(entries, commit_ts="garbage")):
            state = spawn.checkout_state(self.config, self.project, "TASK-2")
        self.assertEqual(state["kind"], "external")
        self.assertIsNone(state["lastCommitAt"])
        self.assertIsNone(state["lastCommitAgeSeconds"])

    def test_checkout_state_reuses_a_precomputed_project_wide_map(self):
        with mock.patch.object(server, "run_git") as run_git:
            state = spawn.checkout_state(
                self.config, self.project, "TASK-2", checkouts={"task/task-2": "/worktrees/my-app-task-2"}
            )
        self.assertEqual(state["kind"], "centrale")
        run_git.assert_not_called()

    def _refusal_mocks(self, entries):
        fake_run_git = mock.MagicMock(side_effect=self._git(entries))
        return dict(
            run_git=mock.patch.object(server, "run_git", fake_run_git),
            run_tmux=mock.patch.object(server, "run_tmux", return_value=tmux_proc([], 0)),
            list_sessions=mock.patch.object(server, "list_sessions", return_value=[]),
            run_backlog=mock.patch.object(server, "run_backlog", return_value=task_view([], status="In Progress")),
            run_backlog_raw=mock.patch.object(server, "run_backlog_raw", return_value=backlog_raw_proc()),
            isdir=mock.patch("os.path.isdir", return_value=False),
            makedirs=mock.patch("os.makedirs"),
        ), fake_run_git

    def test_spawn_refuses_externally_checked_out_branch_before_the_claim(self):
        mocks, fake_run_git = self._refusal_mocks([(FOREIGN_WT, "task/task-2")])
        with mocks["run_git"], mocks["run_tmux"] as run_tmux, mocks["list_sessions"], \
             mocks["run_backlog"], mocks["run_backlog_raw"] as run_backlog_raw, mocks["isdir"], mocks["makedirs"]:
            with self.assertRaises(spawn.SpawnError) as ctx:
                spawn.spawn(self.config, "my-app", "TASK-2")

        self.assertEqual(ctx.exception.status, 409)
        message = str(ctx.exception)
        self.assertIn("task/task-2", message)
        self.assertIn(FOREIGN_WT, message)
        self.assertIn("outside Centrale", message)
        self.assertIn("second checkout", message)
        # No side effect at all: no claim, no commit, no worktree add, no tmux.
        run_backlog_raw.assert_not_called()
        run_tmux.assert_not_called()
        git_subcommands = [c.args[0][:2] for c in fake_run_git.call_args_list]
        self.assertNotIn(["worktree", "add"], git_subcommands)
        self.assertFalse(any(sub[:1] == ["commit"] for sub in git_subcommands))

    def test_resume_refuses_externally_checked_out_branch_before_touching_worktree(self):
        mocks, fake_run_git = self._refusal_mocks([(FOREIGN_WT, "task/task-2")])
        with mocks["run_git"], mocks["run_tmux"] as run_tmux, mocks["list_sessions"], \
             mocks["run_backlog"], mocks["run_backlog_raw"] as run_backlog_raw, mocks["isdir"], mocks["makedirs"]:
            with self.assertRaises(spawn.SpawnError) as ctx:
                spawn.resume(self.config, "my-app", "TASK-2")

        self.assertEqual(ctx.exception.status, 409)
        self.assertIn(FOREIGN_WT, str(ctx.exception))
        run_backlog_raw.assert_not_called()
        run_tmux.assert_not_called()
        git_subcommands = [c.args[0][:2] for c in fake_run_git.call_args_list]
        self.assertNotIn(["worktree", "add"], git_subcommands)

    def test_refusal_applies_under_the_spawn_cmd_override_too(self):
        # Pure git, unlike the Done check -- a hermetic probe spawn onto
        # a branch someone else has checked out is just as impossible.
        mocks, _ = self._refusal_mocks([(FOREIGN_WT, "task/task-2")])
        with mock.patch.dict(os.environ, {"CENTRALE_SPAWN_CMD": "true"}), \
             mocks["run_git"], mocks["run_tmux"] as run_tmux, mocks["list_sessions"], \
             mocks["run_backlog"], mocks["run_backlog_raw"], mocks["isdir"], mocks["makedirs"]:
            with self.assertRaises(spawn.SpawnError) as ctx:
                spawn.spawn(self.config, "my-app", "TASK-2")
        self.assertEqual(ctx.exception.status, 409)
        run_tmux.assert_not_called()

    def test_centrale_managed_checkout_still_resumes(self):
        # The ordinary interrupted-resume path is untouched: the branch is
        # checked out exactly where Centrale expects it.
        entries = [("/repos/my-app", "main"), ("/worktrees/my-app-task-2", "task/task-2")]
        mocks, _ = self._refusal_mocks(entries)
        mocks["isdir"] = mock.patch("os.path.isdir", return_value=True)
        with mocks["run_git"], mocks["run_tmux"] as run_tmux, mocks["list_sessions"], \
             mocks["run_backlog"], mocks["run_backlog_raw"], mocks["isdir"], mocks["makedirs"]:
            result = spawn.resume(self.config, "my-app", "TASK-2")
        self.assertTrue(result["resumed"])
        new_session_argv(run_tmux)  # exactly one session launched

    def test_parked_branch_still_spawns_a_fresh_centrale_worktree(self):
        # kind "none": the branch exists but is checked out nowhere --
        # `git worktree add <dir> <branch>` is exactly right for it.
        entries = [("/repos/my-app", "main")]
        mocks, fake_run_git = self._refusal_mocks(entries)
        with mocks["run_git"], mocks["run_tmux"] as run_tmux, mocks["list_sessions"], \
             mocks["run_backlog"], mocks["run_backlog_raw"], mocks["isdir"], mocks["makedirs"]:
            result = spawn.resume(self.config, "my-app", "TASK-2")
        self.assertTrue(result["resumed"])
        new_session_argv(run_tmux)  # exactly one session launched
        self.assertIn(["worktree", "add"], [c.args[0][:2] for c in fake_run_git.call_args_list])


class DetachedSnapshotTests(unittest.TestCase):
    """Task-79: the throwaway detached worktree harvest's gate 2 and GET
    /api/task's parked-branch "branchTask" now share. Detached means the
    ref is read without ever being checked out (and therefore reserved),
    and the registration plus the directory go away on every path."""

    def setUp(self):
        self.calls = []

    def _fake_git(self, add_returncode=0):
        def fake_run_git(args, cwd=None, **kwargs):
            self.calls.append((list(args), cwd))
            if args[:3] == ["worktree", "add", "--detach"] and add_returncode:
                return git_proc(args, add_returncode, "", "fatal: invalid reference\n")
            return git_proc(args, 0, "", "")
        return fake_run_git

    def test_yields_path_and_removes_registration_and_directory(self):
        with mock.patch.object(server, "run_git", side_effect=self._fake_git()):
            with spawn.detached_snapshot("/repos/my-app", "task/task-2") as (snap, error):
                self.assertIsNone(error)
                self.assertTrue(snap)
                inside = snap

        self.assertEqual(self.calls, [
            (["worktree", "add", "--detach", inside, "task/task-2"], "/repos/my-app"),
            (["worktree", "remove", "--force", inside], "/repos/my-app"),
        ])
        self.assertFalse(os.path.exists(inside))

    def test_add_failure_yields_the_git_error_and_still_removes(self):
        with mock.patch.object(server, "run_git", side_effect=self._fake_git(add_returncode=128)):
            with spawn.detached_snapshot("/repos/my-app", "task/task-2") as (snap, error):
                self.assertIsNone(snap)
                self.assertIn("invalid reference", error)

        # A partial add can still leave a registration behind, so the
        # removal runs even for the failed one.
        self.assertEqual([args[:2] for args, _cwd in self.calls][-1], ["worktree", "remove"])

    def test_cleanup_runs_when_the_body_raises(self):
        with mock.patch.object(server, "run_git", side_effect=self._fake_git()):
            with self.assertRaises(RuntimeError):
                with spawn.detached_snapshot("/repos/my-app", "task/task-2") as (snap, _error):
                    raise RuntimeError("boom")
        self.assertEqual(
            [args[:2] for args, _cwd in self.calls],
            [["worktree", "add"], ["worktree", "remove"]],
        )
        self.assertFalse(os.path.exists(snap))

    def test_snapshot_is_placed_by_tempfile_not_a_hardcoded_path(self):
        # Via tempfile, so TMPDIR (and a test's own override) decides
        # where the scratch tree lands -- it must never be built inside
        # the repo or at a fixed path two callers could collide on.
        tmp_root = tempfile.mkdtemp(prefix="centrale-snapshot-root-")
        self.addCleanup(shutil.rmtree, tmp_root, ignore_errors=True)
        with mock.patch.object(tempfile, "tempdir", tmp_root), \
             mock.patch.object(server, "run_git", side_effect=self._fake_git()):
            with spawn.detached_snapshot("/repos/my-app", "task/task-2") as (snap, _error):
                self.assertEqual(os.path.dirname(snap), tmp_root)


class SpawnHttpApiTests(unittest.TestCase):
    """End-to-end POST /api/spawn tests against a real ThreadingHTTPServer,
    with spawn.spawn mocked so no real git/tmux calls ever happen."""

    @classmethod
    def setUpClass(cls):
        cls.config = make_config(
            "/worktrees", [{"name": "my-app", "path": "/repos/my-app"}]
        )
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

    def test_post_spawn_success(self):
        payload = json.dumps({"project": "my-app", "taskId": "TASK-2"}).encode("utf-8")
        with mock.patch("spawn.spawn", return_value={
            "session": "centrale-my-app-task-2",
            "attach": "tmux attach -t centrale-my-app-task-2",
        }) as fake_spawn:
            status, body = self._post("/api/spawn", payload)

        self.assertEqual(status, 200)
        self.assertEqual(body["session"], "centrale-my-app-task-2")
        self.assertEqual(body["attach"], "tmux attach -t centrale-my-app-task-2")
        fake_spawn.assert_called_once_with(self.config, "my-app", "TASK-2")

    def test_post_spawn_surfaces_spawn_error_status(self):
        import spawn as spawn_module

        payload = json.dumps({"project": "my-app", "taskId": "TASK-2"}).encode("utf-8")
        with mock.patch(
            "spawn.spawn",
            side_effect=spawn_module.SpawnError("session already exists", status=409),
        ):
            status, body = self._post("/api/spawn", payload)

        self.assertEqual(status, 409)
        self.assertIn("error", body)

    def test_post_spawn_malformed_json_returns_400(self):
        status, body = self._post("/api/spawn", b"{not valid json")
        self.assertEqual(status, 400)
        self.assertIn("error", body)

    def test_post_spawn_empty_body_returns_400(self):
        status, body = self._post("/api/spawn", b"")
        self.assertEqual(status, 400)
        self.assertIn("error", body)

    def test_post_unknown_path_returns_404(self):
        status, body = self._post("/api/nope", b"{}")
        self.assertEqual(status, 404)
        self.assertIn("error", body)

    def test_post_resume_success(self):
        payload = json.dumps({"project": "my-app", "taskId": "TASK-2"}).encode("utf-8")
        with mock.patch("spawn.resume", return_value={
            "session": "centrale-my-app-task-2",
            "attach": "tmux attach -t centrale-my-app-task-2",
            "agent": "claude",
            "resumed": True,
        }) as fake_resume:
            status, body = self._post("/api/resume", payload)

        self.assertEqual(status, 200)
        self.assertEqual(body["session"], "centrale-my-app-task-2")
        self.assertTrue(body["resumed"])
        fake_resume.assert_called_once_with(self.config, "my-app", "TASK-2", reconcile=False)

    def test_post_resume_reconcile_flag_is_plumbed_through(self):
        # task-66: the flag rides on the same endpoint; only a literal
        # JSON true turns it on (no truthy strings).
        with mock.patch("spawn.resume", return_value={"session": "s", "attach": "a", "agent": "claude",
                                                      "resumed": True, "reconcile": True}) as fake_resume:
            payload = json.dumps({"project": "my-app", "taskId": "TASK-2", "reconcile": True}).encode("utf-8")
            status, body = self._post("/api/resume", payload)
            self.assertEqual(status, 200)
            self.assertTrue(body["reconcile"])
            fake_resume.assert_called_once_with(self.config, "my-app", "TASK-2", reconcile=True)
        with mock.patch("spawn.resume", return_value={"session": "s", "attach": "a", "agent": "claude",
                                                      "resumed": True}) as fake_resume:
            payload = json.dumps({"project": "my-app", "taskId": "TASK-2", "reconcile": "yes"}).encode("utf-8")
            status, body = self._post("/api/resume", payload)
            self.assertEqual(status, 200)
            self.assertNotIn("reconcile", body)
            fake_resume.assert_called_once_with(self.config, "my-app", "TASK-2", reconcile=False)

    def test_post_resume_surfaces_spawn_error_status(self):
        import spawn as spawn_module

        payload = json.dumps({"project": "my-app", "taskId": "TASK-2"}).encode("utf-8")
        with mock.patch(
            "spawn.resume",
            side_effect=spawn_module.SpawnError("session already exists", status=409),
        ):
            status, body = self._post("/api/resume", payload)

        self.assertEqual(status, 409)
        self.assertIn("error", body)

    def test_post_resume_malformed_json_returns_400(self):
        status, body = self._post("/api/resume", b"{not valid json")
        self.assertEqual(status, 400)
        self.assertIn("error", body)


if __name__ == "__main__":
    unittest.main()
