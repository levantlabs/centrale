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
import settings  # noqa: E402


def _git_result(returncode=0, stdout="true\n", stderr=""):
    return subprocess.CompletedProcess(["git", "rev-parse", "--is-inside-work-tree"], returncode, stdout, stderr)


def _backlog_init_result(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(["backlog", "init"], returncode, stdout, stderr)


def make_config(projects, harvest_mode="click", refresh_interval=10):
    return {
        "port": 0,
        "worktreeRoot": "/tmp/does-not-matter",
        "projects": projects,
        "harvest": {"mode": harvest_mode},
        "refreshIntervalSeconds": refresh_interval,
    }


class CurrentSettingsTests(unittest.TestCase):
    def test_reports_harvest_mode_refresh_interval_and_check_commands(self):
        config = make_config(
            [
                {"name": "my-app", "path": "/repos/my-app", "checkCommand": None},
                {"name": "my-tool", "path": "/repos/my-tool", "checkCommand": "python3 -m unittest discover tests"},
            ],
            harvest_mode="auto",
            refresh_interval=15,
        )
        result = settings.current_settings(config)
        self.assertEqual(result["harvestMode"], "auto")
        self.assertEqual(result["refreshIntervalSeconds"], 15)
        self.assertEqual(
            result["checkCommands"],
            {"my-app": None, "my-tool": "python3 -m unittest discover tests"},
        )

    def test_defaults_when_keys_absent(self):
        config = {"projects": [{"name": "my-app", "path": "/repos/my-app"}]}
        result = settings.current_settings(config)
        self.assertEqual(result["harvestMode"], "click")
        self.assertEqual(result["sessionPreviewMode"], "interact")  # task-60/61: fully enabled by default
        self.assertEqual(result["refreshIntervalSeconds"], settings.DEFAULT_REFRESH_INTERVAL_SECONDS)
        self.assertEqual(result["checkCommands"], {"my-app": None})


class ApplySettingsValidationTests(unittest.TestCase):
    def setUp(self):
        self.config = make_config([
            {"name": "my-app", "path": "/repos/my-app", "checkCommand": None},
            {"name": "my-tool", "path": "/repos/my-tool", "checkCommand": None},
        ])
        tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
        json.dump({"port": 7420, "projects": [
            {"name": "my-app", "path": "~/x/my-app"},
            {"name": "my-tool", "path": "~/x/my-tool"},
        ]}, tmp)
        tmp.close()
        self.tmp_path = tmp.name
        self.addCleanup(lambda: os.path.exists(self.tmp_path) and os.unlink(self.tmp_path))

    def test_rejects_invalid_harvest_mode(self):
        with self.assertRaises(settings.ValidationError) as ctx:
            settings.apply_settings(self.config, {"harvestMode": "sometimes"}, path=self.tmp_path)
        self.assertIn("harvestMode", ctx.exception.fields)

    def test_rejects_invalid_session_preview_mode(self):
        for bad in ("on", "auto", True, None, 1):
            with self.assertRaises(settings.ValidationError) as ctx:
                settings.apply_settings(self.config, {"sessionPreviewMode": bad}, path=self.tmp_path)
            self.assertIn("sessionPreviewMode", ctx.exception.fields)

    def test_rejects_non_integer_refresh_interval(self):
        with self.assertRaises(settings.ValidationError) as ctx:
            settings.apply_settings(self.config, {"refreshIntervalSeconds": "10"}, path=self.tmp_path)
        self.assertIn("refreshIntervalSeconds", ctx.exception.fields)

    def test_rejects_refresh_interval_below_minimum(self):
        with self.assertRaises(settings.ValidationError) as ctx:
            settings.apply_settings(self.config, {"refreshIntervalSeconds": 2}, path=self.tmp_path)
        self.assertIn("refreshIntervalSeconds", ctx.exception.fields)

    def test_rejects_boolean_refresh_interval(self):
        # bool is technically an int subclass in Python -- must not sneak through.
        with self.assertRaises(settings.ValidationError) as ctx:
            settings.apply_settings(self.config, {"refreshIntervalSeconds": True}, path=self.tmp_path)
        self.assertIn("refreshIntervalSeconds", ctx.exception.fields)

    def test_rejects_check_command_for_unknown_project(self):
        with self.assertRaises(settings.ValidationError) as ctx:
            settings.apply_settings(self.config, {"checkCommands": {"ghost": "make test"}}, path=self.tmp_path)
        self.assertIn("checkCommands.ghost", ctx.exception.fields)

    def test_rejects_non_string_check_command(self):
        with self.assertRaises(settings.ValidationError) as ctx:
            settings.apply_settings(self.config, {"checkCommands": {"my-app": 123}}, path=self.tmp_path)
        self.assertIn("checkCommands.my-app", ctx.exception.fields)

    def test_rejects_check_commands_that_is_not_an_object(self):
        with self.assertRaises(settings.SettingsError):
            settings.apply_settings(self.config, {"checkCommands": ["not", "a", "dict"]}, path=self.tmp_path)

    def test_reports_multiple_field_errors_at_once(self):
        with self.assertRaises(settings.ValidationError) as ctx:
            settings.apply_settings(
                self.config,
                {"harvestMode": "bogus", "refreshIntervalSeconds": 1},
                path=self.tmp_path,
            )
        self.assertIn("harvestMode", ctx.exception.fields)
        self.assertIn("refreshIntervalSeconds", ctx.exception.fields)

    def test_invalid_field_never_partially_applies_valid_ones(self):
        original_harvest = dict(self.config["harvest"])
        with self.assertRaises(settings.ValidationError):
            settings.apply_settings(
                self.config,
                {"harvestMode": "auto", "refreshIntervalSeconds": 1},  # valid + invalid together
                path=self.tmp_path,
            )
        # harvestMode was valid on its own but must NOT have been applied,
        # since refreshIntervalSeconds in the same request was invalid.
        self.assertEqual(self.config["harvest"], original_harvest)
        with open(self.tmp_path) as f:
            on_disk = json.load(f)
        self.assertNotIn("harvest", on_disk)


class ApplySettingsSuccessTests(unittest.TestCase):
    def setUp(self):
        self.config = make_config([
            {"name": "my-app", "path": "/repos/my-app", "checkCommand": None},
            {"name": "my-tool", "path": "/repos/my-tool", "checkCommand": "old command"},
        ])
        self.on_disk = {
            "port": 7420,
            "worktreeRoot": "~/x/.worktrees",
            "browserPortBase": 6421,
            "agents": {"claude": ["claude"]},
            "defaultAgent": "claude",
            "projects": [
                {"name": "my-app", "path": "~/x/my-app", "browserPort": 6500},
                {"name": "my-tool", "path": "~/x/my-tool", "checkCommand": "old command"},
            ],
        }
        tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
        json.dump(self.on_disk, tmp)
        tmp.close()
        self.tmp_path = tmp.name
        self.addCleanup(lambda: os.path.exists(self.tmp_path) and os.unlink(self.tmp_path))

    def _read_disk(self):
        with open(self.tmp_path) as f:
            return json.load(f)

    def test_harvest_mode_applies_to_live_config_and_disk(self):
        result = settings.apply_settings(self.config, {"harvestMode": "auto"}, path=self.tmp_path)
        self.assertEqual(result["harvestMode"], "auto")
        self.assertEqual(self.config["harvest"], {"mode": "auto"})
        self.assertEqual(self._read_disk()["harvest"], {"mode": "auto"})

    def test_session_preview_mode_accepts_interact_tier(self):
        # task-61: the reply tier rides the same key.
        result = settings.apply_settings(self.config, {"sessionPreviewMode": "interact"}, path=self.tmp_path)
        self.assertEqual(result["sessionPreviewMode"], "interact")
        self.assertEqual(self.config["sessionPreview"], {"mode": "interact"})
        self.assertEqual(self._read_disk()["sessionPreview"], {"mode": "interact"})

    def test_session_preview_mode_applies_to_live_config_and_disk(self):
        # task-60: stored under projects.json's nested sessionPreview.mode
        # (the same shape load_config reads), not as a flat key.
        result = settings.apply_settings(self.config, {"sessionPreviewMode": "off"}, path=self.tmp_path)
        self.assertEqual(result["sessionPreviewMode"], "off")
        self.assertEqual(self.config["sessionPreview"], {"mode": "off"})
        self.assertEqual(self._read_disk()["sessionPreview"], {"mode": "off"})
        self.assertEqual(server.session_preview_mode(self.config), "off")
        result = settings.apply_settings(self.config, {"sessionPreviewMode": "view"}, path=self.tmp_path)
        self.assertEqual(result["sessionPreviewMode"], "view")
        self.assertEqual(self._read_disk()["sessionPreview"], {"mode": "view"})

    def test_refresh_interval_applies_to_live_config_and_disk(self):
        result = settings.apply_settings(self.config, {"refreshIntervalSeconds": 30}, path=self.tmp_path)
        self.assertEqual(result["refreshIntervalSeconds"], 30)
        self.assertEqual(self.config["refreshIntervalSeconds"], 30)
        self.assertEqual(self._read_disk()["refreshIntervalSeconds"], 30)

    def test_check_command_set_and_cleared(self):
        settings.apply_settings(
            self.config,
            {"checkCommands": {"my-app": "pytest", "my-tool": ""}},  # set one, clear the other
            path=self.tmp_path,
        )
        live_by_name = {p["name"]: p["checkCommand"] for p in self.config["projects"]}
        self.assertEqual(live_by_name["my-app"], "pytest")
        self.assertIsNone(live_by_name["my-tool"])

        disk = self._read_disk()
        disk_by_name = {p["name"]: p for p in disk["projects"]}
        self.assertEqual(disk_by_name["my-app"]["checkCommand"], "pytest")
        self.assertNotIn("checkCommand", disk_by_name["my-tool"])

    def test_omitted_fields_left_untouched(self):
        settings.apply_settings(self.config, {"harvestMode": "auto"}, path=self.tmp_path)
        # refreshIntervalSeconds and checkCommands were never mentioned.
        self.assertEqual(self.config["refreshIntervalSeconds"], 10)
        live_by_name = {p["name"]: p["checkCommand"] for p in self.config["projects"]}
        self.assertEqual(live_by_name["my-tool"], "old command")
        disk = self._read_disk()
        self.assertNotIn("refreshIntervalSeconds", disk)
        disk_by_name = {p["name"]: p for p in disk["projects"]}
        self.assertEqual(disk_by_name["my-tool"]["checkCommand"], "old command")

    def test_non_whitelisted_keys_and_fields_round_trip_untouched(self):
        settings.apply_settings(
            self.config,
            {"harvestMode": "auto", "refreshIntervalSeconds": 20, "checkCommands": {"my-app": "pytest"}},
            path=self.tmp_path,
        )
        disk = self._read_disk()
        self.assertEqual(disk["port"], 7420)
        self.assertEqual(disk["worktreeRoot"], "~/x/.worktrees")
        self.assertEqual(disk["browserPortBase"], 6421)
        self.assertEqual(disk["agents"], {"claude": ["claude"]})
        self.assertEqual(disk["defaultAgent"], "claude")
        disk_by_name = {p["name"]: p for p in disk["projects"]}
        self.assertEqual(disk_by_name["my-app"]["path"], "~/x/my-app")
        self.assertEqual(disk_by_name["my-app"]["browserPort"], 6500)

    def test_write_is_atomic_no_temp_file_left_behind(self):
        settings.apply_settings(self.config, {"harvestMode": "auto"}, path=self.tmp_path)
        directory = os.path.dirname(self.tmp_path)
        leftover = [f for f in os.listdir(directory) if f.startswith(".projects-") and f.endswith(".json.tmp")]
        self.assertEqual(leftover, [])

    def test_missing_projects_json_is_created(self):
        missing_path = self.tmp_path + ".does-not-exist-yet"
        self.addCleanup(lambda: os.path.exists(missing_path) and os.unlink(missing_path))
        settings.apply_settings(self.config, {"harvestMode": "auto"}, path=missing_path)
        with open(missing_path) as f:
            data = json.load(f)
        self.assertEqual(data["harvest"], {"mode": "auto"})


class DefaultAgentTests(unittest.TestCase):
    def setUp(self):
        self.config = make_config([{"name": "my-app", "path": "/repos/my-app", "checkCommand": None}])
        self.config["agents"] = {"claude": {"cmd": ["claude"]}, "codex": {"cmd": ["codex"]}}
        self.config["defaultAgent"] = "claude"
        self.on_disk = {
            "port": 7420,
            "agents": {"claude": ["claude"], "codex": ["codex"]},
            "defaultAgent": "claude",
            "projects": [{"name": "my-app", "path": "~/x/my-app"}],
        }
        tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
        json.dump(self.on_disk, tmp)
        tmp.close()
        self.tmp_path = tmp.name
        self.addCleanup(lambda: os.path.exists(self.tmp_path) and os.unlink(self.tmp_path))

    def test_current_settings_reports_agents_and_default(self):
        result = settings.current_settings(self.config)
        self.assertEqual(result["defaultAgent"], "claude")
        self.assertEqual(result["agents"], ["claude", "codex"])

    def test_valid_default_agent_applies_to_live_config_and_disk(self):
        result = settings.apply_settings(self.config, {"defaultAgent": "codex"}, path=self.tmp_path)
        self.assertEqual(result["defaultAgent"], "codex")
        self.assertEqual(self.config["defaultAgent"], "codex")
        with open(self.tmp_path) as f:
            self.assertEqual(json.load(f)["defaultAgent"], "codex")

    def test_rejects_agent_name_not_in_map(self):
        with self.assertRaises(settings.ValidationError) as ctx:
            settings.apply_settings(self.config, {"defaultAgent": "gpt-nonexistent"}, path=self.tmp_path)
        self.assertIn("defaultAgent", ctx.exception.fields)
        self.assertEqual(self.config["defaultAgent"], "claude")  # unchanged

    def test_rejects_non_string_default_agent(self):
        with self.assertRaises(settings.ValidationError) as ctx:
            settings.apply_settings(self.config, {"defaultAgent": 123}, path=self.tmp_path)
        self.assertIn("defaultAgent", ctx.exception.fields)


class RemoveProjectTests(unittest.TestCase):
    """task-167: what a removal is allowed to refuse, and why.

    The rule this replaced was arithmetic -- "cannot remove the last
    project" -- and it made a stranger's very first project permanent:
    add one with the wrong path on a machine with no projects.json and
    the only way back out was hand-editing the file the settings UI
    exists to spare you. A board with zero projects has been a fully
    supported state since task-53 (it is what every first run shows), so
    the count was never the real question. What a removal can genuinely
    cost is STRANDING work -- it deletes only the projects.json entry, so
    a live session or an unmerged spawn branch would go on existing with
    nothing left in the dashboard able to reach it -- and that is what
    the guard asks about now.
    """

    def setUp(self):
        # Real directories, so the branch probe actually runs against
        # them (a path that does not exist is its own case below).
        self.repo_dirs = {}
        for name in ("my-app", "my-tool"):
            d = tempfile.mkdtemp(prefix=f"centrale-test-{name}-")
            self.addCleanup(shutil.rmtree, d, ignore_errors=True)
            self.repo_dirs[name] = d
        self.config = make_config([
            {"name": "my-app", "path": self.repo_dirs["my-app"], "checkCommand": None},
            {"name": "my-tool", "path": self.repo_dirs["my-tool"], "checkCommand": None},
        ])
        self.on_disk = {
            "port": 7420,
            "projects": [
                {"name": "my-app", "path": "~/x/my-app"},
                {"name": "my-tool", "path": "~/x/my-tool"},
            ],
        }
        tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
        json.dump(self.on_disk, tmp)
        tmp.close()
        self.tmp_path = tmp.name
        self.addCleanup(lambda: os.path.exists(self.tmp_path) and os.unlink(self.tmp_path))

        # Hermetic by default: no tmux server, no unmerged branches.
        # Every test that cares overrides one of the two.
        self.sessions = []
        self.unmerged = []
        self.git_calls = []
        self._tmux = mock.patch.object(server, "run_tmux", side_effect=self._fake_tmux)
        self._tmux.start()
        self.addCleanup(self._tmux.stop)
        self._git = mock.patch.object(server, "run_git", side_effect=self._fake_git)
        self._git.start()
        self.addCleanup(self._git.stop)

    def _fake_tmux(self, args, **kwargs):
        if args and args[0] == "list-sessions":
            if not self.sessions:
                return subprocess.CompletedProcess(
                    ["tmux", *args], 1, "", "no server running on /tmp/tmux-1000/default")
            out = "".join(f"{name}\t1700000000\t0\n" for name in self.sessions)
            return subprocess.CompletedProcess(["tmux", *args], 0, out, "")
        return subprocess.CompletedProcess(["tmux", *args], 0, "", "")

    def _fake_git(self, args, **kwargs):
        self.git_calls.append(list(args))
        if args[:1] == ["symbolic-ref"]:
            return subprocess.CompletedProcess(["git", *args], 0, "main\n", "")
        if args[:1] == ["for-each-ref"]:
            return subprocess.CompletedProcess(
                ["git", *args], 0, "".join(b + "\n" for b in self.unmerged), "")
        return subprocess.CompletedProcess(["git", *args], 0, "", "")

    # -- what is allowed -------------------------------------------------

    def test_removes_from_live_config_and_disk_never_touches_repo(self):
        result = settings.apply_settings(self.config, {"removeProject": "my-app"}, path=self.tmp_path)
        self.assertEqual([p["name"] for p in result["projects"]], ["my-tool"])
        self.assertEqual([p["name"] for p in self.config["projects"]], ["my-tool"])
        with open(self.tmp_path) as f:
            disk = json.load(f)
        self.assertEqual([p["name"] for p in disk["projects"]], ["my-tool"])
        # The repo is only ever READ. The guard has to look at the
        # branches, but a removal still deletes nothing outside
        # projects.json -- no branch, no worktree, no directory.
        for call in self.git_calls:
            self.assertIn(
                call[0], ("symbolic-ref", "rev-parse", "for-each-ref"),
                f"removal ran a git subcommand that is not a read: {call}")

    def test_removes_the_only_configured_project(self):
        """AC #1, and the whole bug: the first project a stranger adds
        must be removable, leaving the zero-project board task-53 already
        supports."""
        single = make_config([
            {"name": "my-app", "path": self.repo_dirs["my-app"], "checkCommand": None},
        ])
        # A projects.json with exactly that one project in it -- the file
        # a stranger has right after adding their first.
        with open(self.tmp_path, "w") as f:
            json.dump({"port": 7420, "projects": [{"name": "my-app", "path": "~/x/my-app"}]}, f)
        result = settings.apply_settings(single, {"removeProject": "my-app"}, path=self.tmp_path)
        self.assertEqual(result["projects"], [])
        self.assertEqual(single["projects"], [])
        self.assertEqual(result["checkCommands"], {})
        with open(self.tmp_path) as f:
            self.assertEqual(json.load(f)["projects"], [])

    def test_a_session_belonging_to_another_project_never_blocks(self):
        self.sessions = ["centrale-my-tool-task-4"]
        settings.apply_settings(self.config, {"removeProject": "my-app"}, path=self.tmp_path)
        self.assertEqual([p["name"] for p in self.config["projects"]], ["my-tool"])

    def test_merged_task_branches_never_block(self):
        # for-each-ref --no-merged is what is asked, so a branch already
        # merged into the checkout's branch simply is not in the answer.
        self.unmerged = []
        settings.apply_settings(self.config, {"removeProject": "my-app"}, path=self.tmp_path)
        self.assertEqual([p["name"] for p in self.config["projects"]], ["my-tool"])

    def test_a_repo_that_is_gone_stays_removable(self):
        """The one a user needs most: the project whose path was wrong,
        or whose directory has since been deleted. Nothing can be
        stranded in a repo that is not there, and a probe that cannot run
        must not become a new dead end."""
        gone = make_config([{"name": "ghost-repo", "path": "/nope/not/here", "checkCommand": None}])
        settings.apply_settings(gone, {"removeProject": "ghost-repo"}, path=self.tmp_path)
        self.assertEqual(gone["projects"], [])
        self.assertEqual(self.git_calls, [])  # nothing to ask, so nothing asked

    def test_an_unanswerable_tmux_never_blocks(self):
        with mock.patch.object(server, "list_sessions", side_effect=server.BacklogError("tmux exploded")):
            settings.apply_settings(self.config, {"removeProject": "my-app"}, path=self.tmp_path)
        self.assertEqual([p["name"] for p in self.config["projects"]], ["my-tool"])

    # -- what is refused, and how it reads -------------------------------

    def test_refuses_while_a_live_session_belongs_to_the_project(self):
        self.sessions = ["centrale-my-app-task-9"]
        with self.assertRaises(settings.ValidationError) as ctx:
            settings.apply_settings(self.config, {"removeProject": "my-app"}, path=self.tmp_path)
        reason = ctx.exception.fields["removeProject"]
        self.assertIn("centrale-my-app-task-9", reason)   # names what is in the way
        self.assertIn("end it from the board", reason)    # and how to clear it
        self.assertNotIn("last project", reason)
        self.assertEqual(len(self.config["projects"]), 2)  # unchanged

    def test_refuses_while_an_unmerged_spawn_branch_is_open(self):
        self.unmerged = ["task/task-7"]
        with self.assertRaises(settings.ValidationError) as ctx:
            settings.apply_settings(self.config, {"removeProject": "my-app"}, path=self.tmp_path)
        reason = ctx.exception.fields["removeProject"]
        self.assertIn("task/task-7", reason)
        self.assertIn("merge, discard or abandon", reason)
        self.assertEqual(len(self.config["projects"]), 2)

    def test_the_refusals_read_correctly_for_one_and_for_several(self):
        """The pronoun has to follow the count. Both messages carry a
        shared tail, so a fixed pronoun in it reads wrong for exactly one
        of the two cases -- and the tail is the half that tells the user
        what to DO, which is the half worth getting right."""
        self.sessions = ["centrale-my-app-task-9", "centrale-my-app-task-10"]
        with self.assertRaises(settings.ValidationError) as ctx:
            settings.apply_settings(self.config, {"removeProject": "my-app"}, path=self.tmp_path)
        reason = ctx.exception.fields["removeProject"]
        self.assertIn("2 live agent sessions are still running", reason)
        self.assertIn("end them from the board", reason)
        self.assertIn("leaves them running", reason)

        self.sessions = []
        self.unmerged = ["task/task-7"]
        with self.assertRaises(settings.ValidationError) as ctx:
            settings.apply_settings(self.config, {"removeProject": "my-app"}, path=self.tmp_path)
        reason = ctx.exception.fields["removeProject"]
        self.assertIn("an unmerged spawn branch is still open", reason)
        self.assertIn("abandon it from the board", reason)

        self.unmerged = ["task/task-7", "task/task-8"]
        with self.assertRaises(settings.ValidationError) as ctx:
            settings.apply_settings(self.config, {"removeProject": "my-app"}, path=self.tmp_path)
        reason = ctx.exception.fields["removeProject"]
        self.assertIn("2 unmerged spawn branches are still open", reason)
        self.assertIn("abandon them from the board", reason)

    def test_the_branch_probe_asks_only_for_unmerged_task_branches(self):
        self.unmerged = ["task/task-7"]
        with self.assertRaises(settings.ValidationError):
            settings.apply_settings(self.config, {"removeProject": "my-app"}, path=self.tmp_path)
        probe = [c for c in self.git_calls if c[0] == "for-each-ref"][-1]
        self.assertIn("--no-merged", probe)
        self.assertIn("main", probe)          # the branch the checkout is on
        self.assertIn("refs/heads/task", probe)

    def test_the_sole_project_is_still_refused_when_it_would_strand_work(self):
        """AC #3 from the other side: dropping the count rule did not
        drop the guard -- it replaced it with one that has a reason."""
        single = make_config([
            {"name": "my-app", "path": self.repo_dirs["my-app"], "checkCommand": None},
        ])
        self.sessions = ["centrale-my-app-task-1"]
        with self.assertRaises(settings.ValidationError):
            settings.apply_settings(single, {"removeProject": "my-app"}, path=self.tmp_path)
        self.assertEqual(len(single["projects"]), 1)

    def test_rejects_unknown_project(self):
        with self.assertRaises(settings.ValidationError) as ctx:
            settings.apply_settings(self.config, {"removeProject": "ghost"}, path=self.tmp_path)
        self.assertIn("removeProject", ctx.exception.fields)
        self.assertEqual(len(self.config["projects"]), 2)  # unchanged

    def test_invalid_removal_never_partially_applies_other_fields(self):
        with self.assertRaises(settings.ValidationError):
            settings.apply_settings(
                self.config,
                {"harvestMode": "auto", "removeProject": "ghost"},
                path=self.tmp_path,
            )
        self.assertEqual(self.config["harvest"], {"mode": "click"})
        with open(self.tmp_path) as f:
            self.assertNotIn("harvest", json.load(f))


class AddProjectTests(unittest.TestCase):
    def setUp(self):
        self.config = make_config([{"name": "my-app", "path": "/repos/my-app", "checkCommand": None}])
        self.on_disk = {"port": 7420, "projects": [{"name": "my-app", "path": "~/x/my-app"}]}
        tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
        json.dump(self.on_disk, tmp)
        tmp.close()
        self.tmp_path = tmp.name
        self.addCleanup(lambda: os.path.exists(self.tmp_path) and os.unlink(self.tmp_path))

        self.repo_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.repo_dir, ignore_errors=True)

    def _with_backlog_config(self):
        os.makedirs(os.path.join(self.repo_dir, "backlog"), exist_ok=True)
        with open(os.path.join(self.repo_dir, "backlog", "config.yml"), "w") as f:
            f.write("statuses: [To Do, In Progress, Done]\n")

    def test_rejects_path_that_does_not_exist(self):
        ghost = os.path.join(self.repo_dir, "does-not-exist")
        with self.assertRaises(settings.ValidationError) as ctx:
            settings.apply_settings(
                self.config, {"addProject": {"name": "newproj", "path": ghost}}, path=self.tmp_path
            )
        self.assertIn("addProject.path", ctx.exception.fields)

    def test_rejects_path_that_is_not_a_git_repo(self):
        with mock.patch.object(server, "run_git", return_value=_git_result(returncode=0, stdout="false\n")):
            with self.assertRaises(settings.ValidationError) as ctx:
                settings.apply_settings(
                    self.config,
                    {"addProject": {"name": "newproj", "path": self.repo_dir}},
                    path=self.tmp_path,
                )
        self.assertIn("addProject.path", ctx.exception.fields)

    def test_rejects_duplicate_name(self):
        with mock.patch.object(server, "run_git", return_value=_git_result()):
            with self.assertRaises(settings.ValidationError) as ctx:
                settings.apply_settings(
                    self.config,
                    {"addProject": {"name": "my-app", "path": self.repo_dir}},
                    path=self.tmp_path,
                )
        self.assertIn("addProject.name", ctx.exception.fields)

    def test_rejects_filesystem_unsafe_name(self):
        with mock.patch.object(server, "run_git", return_value=_git_result()):
            with self.assertRaises(settings.ValidationError) as ctx:
                settings.apply_settings(
                    self.config,
                    {"addProject": {"name": "my project/../evil", "path": self.repo_dir}},
                    path=self.tmp_path,
                )
        self.assertIn("addProject.name", ctx.exception.fields)

    def test_refuses_without_backlog_config_when_init_not_requested(self):
        with mock.patch.object(server, "run_git", return_value=_git_result()) as run_git, \
             mock.patch.object(server, "run_backlog_raw") as run_backlog_raw:
            with self.assertRaises(settings.ValidationError) as ctx:
                settings.apply_settings(
                    self.config,
                    {"addProject": {"name": "newproj", "path": self.repo_dir}},
                    path=self.tmp_path,
                )
        run_backlog_raw.assert_not_called()
        self.assertIn("addProject.path", ctx.exception.fields)
        self.assertEqual(len(self.config["projects"]), 1)  # not added

    def test_init_checkbox_runs_backlog_init_through_injectable_boundary(self):
        with mock.patch.object(server, "run_git", return_value=_git_result()), \
             mock.patch.object(server, "run_backlog_raw", return_value=_backlog_init_result()) as run_backlog_raw:
            result = settings.apply_settings(
                self.config,
                {"addProject": {"name": "newproj", "path": self.repo_dir, "initBacklog": True}},
                path=self.tmp_path,
            )
        run_backlog_raw.assert_called_once()
        args, kwargs = run_backlog_raw.call_args
        self.assertEqual(
            args[0],
            ["init", "newproj", "--defaults", "--integration-mode", "cli", "--agent-instructions", "claude,agents"],
        )
        self.assertEqual(kwargs.get("cwd"), self.repo_dir)
        self.assertIn("newproj", [p["name"] for p in result["projects"]])
        self.assertIn("newproj", [p["name"] for p in self.config["projects"]])
        with open(self.tmp_path) as f:
            disk = json.load(f)
        self.assertIn("newproj", [p["name"] for p in disk["projects"]])

    def test_init_failure_refuses_add_with_cli_message(self):
        with mock.patch.object(server, "run_git", return_value=_git_result()), \
             mock.patch.object(
                 server, "run_backlog_raw",
                 return_value=_backlog_init_result(returncode=1, stderr="boom: already initialized"),
             ):
            with self.assertRaises(settings.ValidationError) as ctx:
                settings.apply_settings(
                    self.config,
                    {"addProject": {"name": "newproj", "path": self.repo_dir, "initBacklog": True}},
                    path=self.tmp_path,
                )
        self.assertIn("boom: already initialized", ctx.exception.fields["addProject.path"])
        self.assertEqual(len(self.config["projects"]), 1)  # not added
        with open(self.tmp_path) as f:
            disk = json.load(f)
        self.assertEqual(len(disk["projects"]), 1)

    def test_succeeds_when_backlog_config_already_present_no_init_needed(self):
        self._with_backlog_config()
        with mock.patch.object(server, "run_git", return_value=_git_result()), \
             mock.patch.object(server, "run_backlog_raw") as run_backlog_raw:
            result = settings.apply_settings(
                self.config,
                {"addProject": {"name": "newproj", "path": self.repo_dir}},
                path=self.tmp_path,
            )
        run_backlog_raw.assert_not_called()
        self.assertIn("newproj", [p["name"] for p in result["projects"]])

    def test_new_project_gets_expanded_path_and_default_fields_in_live_config(self):
        self._with_backlog_config()
        with mock.patch.object(server, "run_git", return_value=_git_result()):
            settings.apply_settings(
                self.config,
                {"addProject": {"name": "newproj", "path": self.repo_dir}},
                path=self.tmp_path,
            )
        added = next(p for p in self.config["projects"] if p["name"] == "newproj")
        self.assertEqual(added["path"], os.path.expanduser(self.repo_dir))
        self.assertIsNone(added["checkCommand"])
        self.assertIsNone(added["browserPort"])

    def test_disk_path_stores_what_user_typed_not_expanded(self):
        self._with_backlog_config()
        typed_path = self.repo_dir  # already absolute in this test; tilde form covered by expanduser elsewhere
        with mock.patch.object(server, "run_git", return_value=_git_result()):
            settings.apply_settings(
                self.config,
                {"addProject": {"name": "newproj", "path": typed_path}},
                path=self.tmp_path,
            )
        with open(self.tmp_path) as f:
            disk = json.load(f)
        disk_entry = next(p for p in disk["projects"] if p["name"] == "newproj")
        self.assertEqual(disk_entry["path"], typed_path)

    def test_invalid_add_never_partially_applies_other_fields(self):
        with self.assertRaises(settings.ValidationError):
            settings.apply_settings(
                self.config,
                {"harvestMode": "auto", "addProject": {"name": "my-app", "path": self.repo_dir}},  # dup name
                path=self.tmp_path,
            )
        self.assertEqual(self.config["harvest"], {"mode": "click"})
        self.assertEqual(len(self.config["projects"]), 1)
        with open(self.tmp_path) as f:
            disk = json.load(f)
        self.assertNotIn("harvest", disk)
        self.assertEqual(len(disk["projects"]), 1)

    def test_missing_name_and_path_reported_together(self):
        with self.assertRaises(settings.ValidationError) as ctx:
            settings.apply_settings(self.config, {"addProject": {}}, path=self.tmp_path)
        self.assertIn("addProject.name", ctx.exception.fields)
        self.assertIn("addProject.path", ctx.exception.fields)

    def test_add_project_not_an_object_is_rejected(self):
        with self.assertRaises(settings.ValidationError) as ctx:
            settings.apply_settings(self.config, {"addProject": "my-app"}, path=self.tmp_path)
        self.assertIn("addProject", ctx.exception.fields)


class ZeroConfigFirstRunFlowTests(unittest.TestCase):
    """task-53 end to end: server.load_config() on a genuinely missing
    projects.json (a fresh clone's first boot) hands back a zero-config
    default config, and the very first settings.apply_settings() save
    against that same path -- the Add Project flow the empty-state UI
    points at -- creates the file from scratch. Ties together the two
    halves of the zero-config story (server.py's boot-time defaults,
    settings.py's already-existing atomic-write-creates-the-file
    behavior) rather than testing each in isolation."""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="centrale-test-zero-config-")
        self.addCleanup(shutil.rmtree, self.tmp_dir, ignore_errors=True)
        self.config_path = os.path.join(self.tmp_dir, "projects.json")  # deliberately never created
        self.repo_dir = os.path.join(self.tmp_dir, "repo")
        os.makedirs(os.path.join(self.repo_dir, "backlog"))
        with open(os.path.join(self.repo_dir, "backlog", "config.yml"), "w") as f:
            f.write("statuses: [To Do, In Progress, Done]\n")

    def test_boot_then_add_project_creates_the_file_with_the_new_project(self):
        self.assertFalse(os.path.exists(self.config_path))

        config = server.load_config(path=self.config_path)
        self.assertTrue(config["zeroConfig"])
        self.assertEqual(config["projects"], [])
        self.assertEqual(config["worktreeRoot"], server.WORKTREE_ROOT_REPO_TOKEN)

        with mock.patch.object(server, "run_git", return_value=_git_result()), \
             mock.patch.object(server, "run_backlog_raw") as run_backlog_raw:
            result = settings.apply_settings(
                config,
                {"addProject": {"name": "newproj", "path": self.repo_dir}},
                path=self.config_path,
            )
        run_backlog_raw.assert_not_called()  # backlog/config.yml already present -- no init needed

        self.assertTrue(os.path.isfile(self.config_path))
        self.assertIn("newproj", [p["name"] for p in result["projects"]])
        self.assertIn("newproj", [p["name"] for p in config["projects"]])  # live config updated too

        # Reloading from the now-real file must no longer read as
        # zero-config, must see the newly added project, and -- since
        # '@repo' is now normalize_worktree_root's unconditional
        # fallback for an omitted key, not something zero-config mode
        # specifically stamped in (see addendum to task-53) -- must
        # still resolve to '@repo' even though the file on disk never
        # got a worktreeRoot key written into it at all.
        reloaded = server.load_config(path=self.config_path)
        self.assertFalse(reloaded["zeroConfig"])
        self.assertIn("newproj", [p["name"] for p in reloaded["projects"]])
        self.assertEqual(reloaded["worktreeRoot"], server.WORKTREE_ROOT_REPO_TOKEN)

    def test_add_then_remove_returns_the_board_to_the_first_run_state(self):
        """task-167, AC #1/#2: the whole first-run round trip on one
        path. A stranger boots with no projects.json, adds their first
        project, gets the path wrong -- and can take it back out again,
        landing on the zero-project board the same boot already showed
        them. `--check` over the resulting file still passes, with the
        same "add one from the settings gear" guidance a first run
        gives, because a board with zero projects is a supported state
        and not a broken one."""
        config = server.load_config(path=self.config_path)

        with mock.patch.object(server, "run_git", return_value=_git_result()):
            settings.apply_settings(
                config,
                {"addProject": {"name": "newproj", "path": self.repo_dir}},
                path=self.config_path,
            )
        self.assertEqual([p["name"] for p in config["projects"]], ["newproj"])

        # No tmux server, and the fresh repo has no task/* branches: the
        # removal strands nothing, so it goes through.
        no_tmux = subprocess.CompletedProcess(["tmux"], 1, "", "no server running")
        with mock.patch.object(server, "run_tmux", return_value=no_tmux), \
             mock.patch.object(server, "run_git", return_value=_git_result(stdout="")):
            result = settings.apply_settings(config, {"removeProject": "newproj"}, path=self.config_path)

        self.assertEqual(result["projects"], [])
        self.assertEqual(config["projects"], [])
        with open(self.config_path) as f:
            self.assertEqual(json.load(f)["projects"], [])

        # The board renders its empty state off exactly this: no
        # projects in the payload (static/board.js's renderBoard), the
        # same as on a boot with no file at all.
        reloaded = server.load_config(path=self.config_path)
        self.assertEqual(reloaded["projects"], [])

        def which(name):
            return None if name == "bwrap" else f"/usr/bin/{name}"

        with mock.patch.object(server, "which", side_effect=which), \
             mock.patch.object(server, "run_git",
                               return_value=subprocess.CompletedProcess(["git"], 0, "git version 2.43.0\n", "")), \
             mock.patch.object(server, "run_backlog_raw",
                               return_value=subprocess.CompletedProcess(["backlog"], 0, "backlog/1.50.1\n", "")):
            lines, ok = server.run_doctor_check(config_path=self.config_path)
        self.assertTrue(ok)
        self.assertEqual([line for line in lines if line.startswith("[FAIL]")], [])
        self.assertTrue(
            any(line.startswith("[WARN]") and "no projects configured" in line for line in lines),
            lines)

    def test_save_never_writes_a_worktree_root_key_of_its_own(self):
        # worktreeRoot isn't one of apply_settings' whitelisted fields --
        # a save (first or otherwise) never invents one on disk. An
        # omitted key resolving to '@repo' is entirely
        # normalize_worktree_root's own default (see the test above),
        # not something settings.py writes; a user's own explicit choice
        # (set by hand, or a future settings field) is likewise never
        # touched by an unrelated save.
        config = server.load_config(path=self.config_path)
        settings.apply_settings(config, {"harvestMode": "auto"}, path=self.config_path)  # creates the file
        with open(self.config_path) as f:
            disk = json.load(f)
        self.assertNotIn("worktreeRoot", disk)

        disk["worktreeRoot"] = "~/custom-worktrees"
        with open(self.config_path, "w") as f:
            json.dump(disk, f)

        config2 = server.load_config(path=self.config_path)
        settings.apply_settings(config2, {"harvestMode": "click"}, path=self.config_path)
        with open(self.config_path) as f:
            disk2 = json.load(f)
        self.assertEqual(disk2["worktreeRoot"], "~/custom-worktrees")


class WhitelistDisciplineTests(unittest.TestCase):
    """New fields (defaultAgent/addProject/removeProject) must still leave
    everything outside the whitelist -- the agents map itself, port,
    worktreeRoot, browserPortBase -- completely untouched, same guarantee
    the pre-existing settings already had."""

    def setUp(self):
        self.config = make_config([
            {"name": "my-app", "path": "/repos/my-app", "checkCommand": None},
            {"name": "my-tool", "path": "/repos/my-tool", "checkCommand": None},
        ])
        self.config["agents"] = {"claude": {"cmd": ["claude"]}}
        self.config["defaultAgent"] = "claude"
        self.on_disk = {
            "port": 7420,
            "worktreeRoot": "~/x/.worktrees",
            "browserPortBase": 6421,
            "agents": {"claude": ["claude"]},
            "defaultAgent": "claude",
            "projects": [
                {"name": "my-app", "path": "~/x/my-app", "browserPort": 6500},
                {"name": "my-tool", "path": "~/x/my-tool"},
            ],
        }
        tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
        json.dump(self.on_disk, tmp)
        tmp.close()
        self.tmp_path = tmp.name
        self.addCleanup(lambda: os.path.exists(self.tmp_path) and os.unlink(self.tmp_path))

    def _read_disk(self):
        with open(self.tmp_path) as f:
            return json.load(f)

    def test_remove_project_leaves_unrelated_keys_untouched(self):
        with mock.patch.object(server, "run_git"):
            settings.apply_settings(self.config, {"removeProject": "my-tool"}, path=self.tmp_path)
        disk = self._read_disk()
        self.assertEqual(disk["port"], 7420)
        self.assertEqual(disk["worktreeRoot"], "~/x/.worktrees")
        self.assertEqual(disk["browserPortBase"], 6421)
        self.assertEqual(disk["agents"], {"claude": ["claude"]})
        remaining = next(p for p in disk["projects"] if p["name"] == "my-app")
        self.assertEqual(remaining["browserPort"], 6500)  # other per-project fields round-trip untouched

    def test_default_agent_change_leaves_agents_map_untouched(self):
        self.config["agents"]["codex"] = {"cmd": ["codex"]}
        self.on_disk["agents"]["codex"] = ["codex"]
        with open(self.tmp_path, "w") as f:
            json.dump(self.on_disk, f)
        settings.apply_settings(self.config, {"defaultAgent": "codex"}, path=self.tmp_path)
        disk = self._read_disk()
        self.assertEqual(disk["agents"], {"claude": ["claude"], "codex": ["codex"]})
        self.assertEqual(disk["defaultAgent"], "codex")


class _SettingsHttpBase(unittest.TestCase):
    """Shared plumbing: a real CentraleHTTPServer on 127.0.0.1:0 whose
    settings writes go to a throwaway file."""

    @classmethod
    def setUpClass(cls):
        cls.config = make_config([
            {"name": "my-app", "path": "/repos/my-app", "checkCommand": None},
            {"name": "my-tool", "path": "/repos/my-tool", "checkCommand": None},
        ])
        cls.config["agents"] = {"claude": {"cmd": ["claude"]}, "codex": {"cmd": ["codex"]}}
        cls.config["defaultAgent"] = "claude"
        cls.httpd = server.CentraleHTTPServer(("127.0.0.1", 0), server.Handler, cls.config)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join(timeout=5)

    def setUp(self):
        tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
        json.dump({
            "projects": [
                {"name": "my-app", "path": "~/x/my-app"},
                {"name": "my-tool", "path": "~/x/my-tool"},
            ],
            "agents": {"claude": ["claude"], "codex": ["codex"]},
            "defaultAgent": "claude",
        }, tmp)
        tmp.close()
        self.tmp_path = tmp.name
        self.addCleanup(lambda: os.path.exists(self.tmp_path) and os.unlink(self.tmp_path))
        # Every test's writes go to a throwaway file, never the real projects.json.
        self._config_path_patch = mock.patch.object(server, "DEFAULT_CONFIG_PATH", self.tmp_path)
        self._config_path_patch.start()
        self.addCleanup(self._config_path_patch.stop)
        self.config["harvest"] = {"mode": "click"}
        self.config["refreshIntervalSeconds"] = 10
        self.config["defaultAgent"] = "claude"
        self.config["projects"] = [
            {"name": "my-app", "path": "/repos/my-app", "checkCommand": None},
            {"name": "my-tool", "path": "/repos/my-tool", "checkCommand": None},
        ]
        # task-167: removing a project now asks tmux whether one of its
        # agent sessions is still live. These tests run a real HTTP
        # server in-process, so that question has to be answered here or
        # it reaches the developer's own tmux. Default: no tmux server
        # at all; a test that wants a session appends its name.
        self.tmux_sessions = []
        tmux_patch = mock.patch.object(server, "run_tmux", side_effect=self._fake_tmux)
        tmux_patch.start()
        self.addCleanup(tmux_patch.stop)

    def _fake_tmux(self, args, **kwargs):
        if args and args[0] == "list-sessions":
            if not self.tmux_sessions:
                return subprocess.CompletedProcess(
                    ["tmux", *args], 1, "", "no server running on /tmp/tmux-1000/default")
            out = "".join(f"{name}\t1700000000\t0\n" for name in self.tmux_sessions)
            return subprocess.CompletedProcess(["tmux", *args], 0, out, "")
        return subprocess.CompletedProcess(["tmux", *args], 0, "", "")

    def _url(self, path):
        return f"http://127.0.0.1:{self.port}{path}"

    def _get(self, path):
        try:
            with urllib.request.urlopen(self._url(path), timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def _post(self, path, body_bytes):
        req = urllib.request.Request(
            self._url(path), data=body_bytes, method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))


class SettingsHttpApiTests(_SettingsHttpBase):
    def test_get_settings_returns_current_values(self):
        status, body = self._get("/api/settings")
        self.assertEqual(status, 200)
        self.assertEqual(body["harvestMode"], "click")
        self.assertEqual(body["refreshIntervalSeconds"], 10)
        self.assertEqual(body["checkCommands"], {"my-app": None, "my-tool": None})
        self.assertEqual(body["defaultAgent"], "claude")
        self.assertEqual(body["agents"], ["claude", "codex"])
        self.assertEqual(
            body["projects"],
            [{"name": "my-app", "path": "/repos/my-app"}, {"name": "my-tool", "path": "/repos/my-tool"}],
        )

    def test_post_session_preview_off_then_endpoint_refuses_without_tmux(self):
        # task-60 AC #4 end to end: flipping the toggle through the
        # settings API must make GET /api/session-pane refuse cleanly
        # (403) without ever running tmux, and flipping it back re-enables.
        self.addCleanup(lambda: self.config.pop("sessionPreview", None))
        status, body = self._post("/api/settings", json.dumps({"sessionPreviewMode": "off"}).encode())
        self.assertEqual(status, 200)
        self.assertEqual(body["sessionPreviewMode"], "off")
        status, body = self._get("/api/settings")
        self.assertEqual(body["sessionPreviewMode"], "off")
        with open(self.tmp_path) as f:
            self.assertEqual(json.load(f)["sessionPreview"], {"mode": "off"})

        with mock.patch.object(server, "run_tmux", side_effect=AssertionError("tmux must not run when disabled")):
            status, body = self._get("/api/session-pane?project=my-app&task=TASK-9")
        self.assertEqual(status, 403)
        self.assertIn("disabled", body["error"])

        status, body = self._post("/api/settings", json.dumps({"sessionPreviewMode": "view"}).encode())
        self.assertEqual(status, 200)
        capture = subprocess.CompletedProcess(["tmux"], 0, "hello\n", "")
        with mock.patch.object(server, "run_tmux", return_value=capture):
            status, body = self._get("/api/session-pane?project=my-app&task=TASK-9")
        self.assertEqual(status, 200)
        self.assertEqual(body["lines"], ["hello"])

    def test_post_session_preview_view_disables_reply_but_keeps_pane(self):
        # task-61 AC #4 end to end: the settings API can turn the reply
        # off ("view") while the read-only pane keeps working, and back on
        # ("interact"). The reply endpoint refuses without running tmux.
        self.addCleanup(lambda: self.config.pop("sessionPreview", None))
        self.addCleanup(server._reset_pane_capture_times)
        status, body = self._post("/api/settings", json.dumps({"sessionPreviewMode": "view"}).encode())
        self.assertEqual(status, 200)
        self.assertEqual(body["sessionPreviewMode"], "view")
        with open(self.tmp_path) as f:
            self.assertEqual(json.load(f)["sessionPreview"], {"mode": "view"})

        capture = subprocess.CompletedProcess(["tmux"], 0, "? proceed (y/n)\n", "")
        with mock.patch.object(server, "run_tmux", return_value=capture):
            status, body = self._get("/api/session-pane?project=my-app&task=TASK-9")
        self.assertEqual(status, 200)
        self.assertEqual(body["lines"], ["? proceed (y/n)"])
        with mock.patch.object(server, "run_tmux", side_effect=AssertionError("tmux must not run when reply is disabled")):
            status, body = self._post(
                "/api/session-input",
                json.dumps({"project": "my-app", "taskId": "TASK-9", "text": "yes"}).encode(),
            )
        self.assertEqual(status, 403)
        self.assertIn("disabled", body["error"])

        status, body = self._post("/api/settings", json.dumps({"sessionPreviewMode": "interact"}).encode())
        self.assertEqual(status, 200)
        self.assertEqual(body["sessionPreviewMode"], "interact")
        calls = []

        def fake(args, input=None):
            calls.append(list(args))
            return subprocess.CompletedProcess(["tmux", *args], 0, "", "")

        with mock.patch.object(server, "_new_paste_buffer_name", return_value="centrale-reply-test"), \
                mock.patch.object(server, "run_tmux", side_effect=fake):
            status, body = self._post(
                "/api/session-input",
                json.dumps({"project": "my-app", "taskId": "TASK-9", "text": "yes"}).encode(),
            )
        self.assertEqual(status, 200)
        self.assertEqual(calls, [
            ["load-buffer", "-b", "centrale-reply-test", "-"],
            [
                "paste-buffer", "-d", "-p", "-b", "centrale-reply-test",
                "-t", "=centrale-my-app-task-9:",
            ],
            ["send-keys", "-t", "=centrale-my-app-task-9:", "Enter"],
        ])

    def test_post_settings_success(self):
        payload = json.dumps({"harvestMode": "auto", "refreshIntervalSeconds": 20}).encode("utf-8")
        status, body = self._post("/api/settings", payload)
        self.assertEqual(status, 200)
        self.assertEqual(body["harvestMode"], "auto")
        self.assertEqual(body["refreshIntervalSeconds"], 20)
        # And GET reflects it immediately -- no restart needed.
        status2, body2 = self._get("/api/settings")
        self.assertEqual(body2["harvestMode"], "auto")

    def test_post_settings_validation_error_returns_field_errors(self):
        payload = json.dumps({"harvestMode": "bogus"}).encode("utf-8")
        status, body = self._post("/api/settings", payload)
        self.assertEqual(status, 400)
        self.assertIn("error", body)
        self.assertIn("harvestMode", body.get("fields", {}))

    def test_post_settings_malformed_body_returns_400(self):
        status, body = self._post("/api/settings", b"{not valid json")
        self.assertEqual(status, 400)
        self.assertIn("error", body)

    def test_post_default_agent_success(self):
        payload = json.dumps({"defaultAgent": "codex"}).encode("utf-8")
        status, body = self._post("/api/settings", payload)
        self.assertEqual(status, 200)
        self.assertEqual(body["defaultAgent"], "codex")
        status2, body2 = self._get("/api/settings")
        self.assertEqual(body2["defaultAgent"], "codex")

    def test_post_default_agent_rejects_unknown_name(self):
        payload = json.dumps({"defaultAgent": "gpt-nonexistent"}).encode("utf-8")
        status, body = self._post("/api/settings", payload)
        self.assertEqual(status, 400)
        self.assertIn("defaultAgent", body.get("fields", {}))

    def test_post_remove_project_success(self):
        payload = json.dumps({"removeProject": "my-tool"}).encode("utf-8")
        status, body = self._post("/api/settings", payload)
        self.assertEqual(status, 200)
        self.assertEqual([p["name"] for p in body["projects"]], ["my-app"])
        status2, body2 = self._get("/api/settings")
        self.assertEqual([p["name"] for p in body2["projects"]], ["my-app"])

    def test_post_remove_project_can_empty_the_board(self):
        """AC #1 over the HTTP route: removing down to one project and
        then removing that one leaves a zero-project board, the state a
        first run already shows. Until task-167 the second POST was a
        400 and the settings UI had no way to undo a first project added
        with the wrong path."""
        status, _ = self._post("/api/settings", json.dumps({"removeProject": "my-tool"}).encode("utf-8"))
        self.assertEqual(status, 200)
        status, body = self._post("/api/settings", json.dumps({"removeProject": "my-app"}).encode("utf-8"))
        self.assertEqual(status, 200)
        self.assertEqual(body["projects"], [])
        status2, body2 = self._get("/api/settings")
        self.assertEqual(body2["projects"], [])
        self.assertEqual(body2["checkCommands"], {})
        with open(self.tmp_path) as f:
            self.assertEqual(json.load(f)["projects"], [])

    def test_post_remove_project_refuses_one_with_a_live_session(self):
        """The guard that replaced the count rule, over the route: a
        reason about work this would strand, naming the session."""
        self.tmux_sessions = ["centrale-my-app-task-9"]
        status, body = self._post("/api/settings", json.dumps({"removeProject": "my-app"}).encode("utf-8"))
        self.assertEqual(status, 400)
        reason = body.get("fields", {}).get("removeProject", "")
        self.assertIn("centrale-my-app-task-9", reason)
        status2, body2 = self._get("/api/settings")
        self.assertEqual([p["name"] for p in body2["projects"]], ["my-app", "my-tool"])

    def test_post_add_project_success_through_injectable_boundaries(self):
        repo_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, repo_dir, ignore_errors=True)
        os.makedirs(os.path.join(repo_dir, "backlog"), exist_ok=True)
        with open(os.path.join(repo_dir, "backlog", "config.yml"), "w") as f:
            f.write("statuses: [To Do, Done]\n")

        with mock.patch.object(server, "run_git", return_value=_git_result()):
            payload = json.dumps({"addProject": {"name": "newproj", "path": repo_dir}}).encode("utf-8")
            status, body = self._post("/api/settings", payload)
        self.assertEqual(status, 200)
        self.assertIn("newproj", [p["name"] for p in body["projects"]])
        status2, body2 = self._get("/api/settings")
        self.assertIn("newproj", [p["name"] for p in body2["projects"]])

    def test_post_add_project_rejects_non_git_path(self):
        repo_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, repo_dir, ignore_errors=True)
        with mock.patch.object(server, "run_git", return_value=_git_result(returncode=1, stdout="")):
            payload = json.dumps({"addProject": {"name": "newproj", "path": repo_dir}}).encode("utf-8")
            status, body = self._post("/api/settings", payload)
        self.assertEqual(status, 400)
        self.assertIn("addProject.path", body.get("fields", {}))


if __name__ == "__main__":
    unittest.main()


# ----------------------------------------------------------------------
# task-78: the Agents editor -- the whole-map `agents` field
# ----------------------------------------------------------------------

def _task_view_proc(assignees):
    """A `backlog task view --json` result for spawn.resolve_agent."""
    return {"schemaVersion": 1, "kind": "task-view", "task": {"id": "TASK-2", "assignees": assignees}}


class AgentsEditorTests(unittest.TestCase):
    """apply_settings' `agents` field: the settings modal's Agents editor
    posts the whole map as an ordered list of {name, cmd, promptSuffix};
    the server validates, writes projects.json's "agents" through the
    same atomic path as every other setting, and swaps the live map so
    the very next spawn resolves it. `which` is patched throughout so
    the suite never depends on what's installed on the machine."""

    SPACED_CMD = ["claude", "--append-system-prompt", "You are a layout specialist."]

    def setUp(self):
        self.on_disk = {
            "port": 7420,
            "defaultAgent": "claude",
            "agents": {
                "claude": ["claude"],
                "codex": ["codex"],
                "reviewer": {
                    "cmd": list(self.SPACED_CMD),
                    "promptSuffix": "Prefer the layout-reviewer subagent.",
                    "resumeCmd": ["claude", "--continue"],
                },
            },
            "projects": [{"name": "my-app", "path": "~/x/my-app"}],
        }
        tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
        json.dump(self.on_disk, tmp)
        tmp.close()
        self.tmp_path = tmp.name
        self.addCleanup(lambda: os.path.exists(self.tmp_path) and os.unlink(self.tmp_path))
        self.config = server.load_config(path=self.tmp_path)
        # Everything is "installed" unless a test says otherwise.
        self._which = mock.patch.object(server, "which", side_effect=lambda name: "/usr/bin/" + os.path.basename(name))
        self._which.start()
        self.addCleanup(self._which.stop)

    def _disk(self):
        with open(self.tmp_path) as f:
            return json.load(f)

    def _rows(self):
        """The editor's own payload for the current config: what a Save
        with an untouched-but-dirty section would post."""
        return [
            {"name": e["name"], "cmd": e["cmdText"], "promptSuffix": e["promptSuffix"] or ""}
            for e in settings.agent_entries(self.config)
        ]

    # -- GET shape ---------------------------------------------------

    def test_current_settings_reports_agent_entries_in_map_order(self):
        result = settings.current_settings(self.config)
        entries = result["agentEntries"]
        self.assertEqual([e["name"] for e in entries], ["claude", "codex", "reviewer"])
        self.assertEqual(entries[0], {
            "name": "claude", "cmd": ["claude"], "cmdText": "claude", "promptSuffix": None,
            "builtin": True, "onPath": True,
        })
        self.assertTrue(entries[1]["builtin"])
        reviewer = entries[2]
        self.assertFalse(reviewer["builtin"])
        self.assertEqual(reviewer["cmd"], self.SPACED_CMD)
        self.assertEqual(reviewer["cmdText"], "claude --append-system-prompt 'You are a layout specialist.'")
        self.assertEqual(reviewer["promptSuffix"], "Prefer the layout-reviewer subagent.")
        # The plain names list every existing caller reads is still there.
        self.assertEqual(result["agents"], ["claude", "codex", "reviewer"])

    def test_on_path_reflects_which_through_the_injectable_boundary(self):
        with mock.patch.object(server, "which", side_effect=lambda name: None if name == "codex" else "/usr/bin/x"):
            entries = {e["name"]: e for e in settings.agent_entries(self.config)}
        self.assertTrue(entries["claude"]["onPath"])
        self.assertFalse(entries["codex"]["onPath"])

    # -- round trip --------------------------------------------------

    def test_argv_round_trips_exactly_through_cmd_text(self):
        # The editor shows shlex.join(argv) and posts the edited text
        # back; an argument containing spaces must come back as ONE argv
        # element, byte for byte.
        settings.apply_settings(self.config, {"agents": self._rows()}, path=self.tmp_path)
        self.assertEqual(self.config["agents"]["reviewer"]["cmd"], self.SPACED_CMD)
        self.assertEqual(self._disk()["agents"]["reviewer"]["cmd"], self.SPACED_CMD)
        self.assertEqual(self._disk()["agents"]["claude"], ["claude"])

    def test_cmd_accepts_an_argv_list_verbatim(self):
        rows = self._rows()
        rows.append({"name": "bot", "cmd": ["my agent", "--x y"], "promptSuffix": None})
        settings.apply_settings(self.config, {"agents": rows}, path=self.tmp_path)
        self.assertEqual(self.config["agents"]["bot"]["cmd"], ["my agent", "--x y"])
        self.assertEqual(self._disk()["agents"]["bot"], ["my agent", "--x y"])

    def test_untouched_save_leaves_the_map_byte_identical(self):
        before = self._disk()["agents"]
        settings.apply_settings(self.config, {"agents": self._rows()}, path=self.tmp_path)
        self.assertEqual(self._disk()["agents"], before)

    def test_editing_an_object_entry_keeps_its_other_keys(self):
        # A hand-written resumeCmd is not an editor field; a save that
        # changes the command must not lose it -- on disk or live.
        rows = self._rows()
        rows[2]["cmd"] = "claude --model opus"
        settings.apply_settings(self.config, {"agents": rows}, path=self.tmp_path)
        disk_entry = self._disk()["agents"]["reviewer"]
        self.assertEqual(disk_entry["cmd"], ["claude", "--model", "opus"])
        self.assertEqual(disk_entry["resumeCmd"], ["claude", "--continue"])
        self.assertEqual(disk_entry["promptSuffix"], "Prefer the layout-reviewer subagent.")
        self.assertEqual(self.config["agents"]["reviewer"]["resumeCmd"], ["claude", "--continue"])

    # -- add / edit / delete -----------------------------------------

    def test_add_agent_with_and_without_suffix_uses_the_lightest_file_form(self):
        rows = self._rows()
        rows.append({"name": "plain", "cmd": "my-wrapper --fast", "promptSuffix": "  "})
        rows.append({"name": "steered", "cmd": "claude", "promptSuffix": "Be brief."})
        result = settings.apply_settings(self.config, {"agents": rows}, path=self.tmp_path)
        disk = self._disk()["agents"]
        self.assertEqual(disk["plain"], ["my-wrapper", "--fast"])  # blank suffix -> plain list
        self.assertEqual(disk["steered"], {"cmd": ["claude"], "promptSuffix": "Be brief."})
        self.assertEqual(list(disk.keys()), ["claude", "codex", "reviewer", "plain", "steered"])
        self.assertEqual(self.config["agents"]["plain"], {"cmd": ["my-wrapper", "--fast"], "promptSuffix": None, "resumeCmd": None})
        self.assertEqual(self.config["agents"]["steered"]["promptSuffix"], "Be brief.")
        self.assertEqual([e["name"] for e in result["agentEntries"]], ["claude", "codex", "reviewer", "plain", "steered"])
        self.assertEqual(result["warnings"], [])

    def test_builtin_command_is_overridable(self):
        rows = self._rows()
        rows[0]["cmd"] = "claude --model opus"
        settings.apply_settings(self.config, {"agents": rows}, path=self.tmp_path)
        self.assertEqual(self._disk()["agents"]["claude"], ["claude", "--model", "opus"])
        self.assertEqual(self.config["agents"]["claude"]["cmd"], ["claude", "--model", "opus"])

    def test_clearing_a_suffix_removes_it_from_the_object_entry(self):
        rows = self._rows()
        rows[2]["promptSuffix"] = ""
        settings.apply_settings(self.config, {"agents": rows}, path=self.tmp_path)
        self.assertNotIn("promptSuffix", self._disk()["agents"]["reviewer"])
        self.assertIsNone(self.config["agents"]["reviewer"]["promptSuffix"])

    def test_delete_user_agent_removes_it_from_disk_and_live(self):
        rows = [r for r in self._rows() if r["name"] != "reviewer"]
        settings.apply_settings(self.config, {"agents": rows}, path=self.tmp_path)
        self.assertNotIn("reviewer", self._disk()["agents"])
        self.assertNotIn("reviewer", self.config["agents"])
        self.assertEqual(sorted(self.config["agents"]), ["claude", "codex"])

    def test_first_save_on_a_config_running_on_defaults_writes_the_map(self):
        with open(self.tmp_path, "w") as f:
            json.dump({"projects": [{"name": "my-app", "path": "~/x/my-app"}]}, f)
        config = server.load_config(path=self.tmp_path)
        self.assertNotIn("agents", self._disk())
        rows = [{"name": e["name"], "cmd": e["cmdText"], "promptSuffix": ""} for e in settings.agent_entries(config)]
        rows.append({"name": "bot", "cmd": "bot", "promptSuffix": ""})
        settings.apply_settings(config, {"agents": rows}, path=self.tmp_path)
        self.assertEqual(self._disk()["agents"], {"claude": ["claude"], "codex": ["codex"], "bot": ["bot"]})

    def test_saves_without_an_agents_field_never_invent_the_key(self):
        with open(self.tmp_path, "w") as f:
            json.dump({"projects": [{"name": "my-app", "path": "~/x/my-app"}]}, f)
        config = server.load_config(path=self.tmp_path)
        settings.apply_settings(config, {"harvestMode": "auto", "defaultAgent": "codex"}, path=self.tmp_path)
        self.assertNotIn("agents", self._disk())

    # -- validation --------------------------------------------------

    def _assert_nothing_changed(self):
        self.assertEqual(self._disk(), self.on_disk)
        self.assertEqual(sorted(self.config["agents"]), ["claude", "codex", "reviewer"])
        self.assertEqual(self.config["defaultAgent"], "claude")

    def test_empty_name_and_empty_command_are_blocked_by_index(self):
        rows = self._rows()
        rows.append({"name": "  ", "cmd": "bot", "promptSuffix": ""})
        rows.append({"name": "bot2", "cmd": "   ", "promptSuffix": ""})
        rows.append({"name": "bot3", "cmd": [], "promptSuffix": ""})
        with self.assertRaises(settings.ValidationError) as ctx:
            settings.apply_settings(self.config, {"agents": rows, "harvestMode": "auto"}, path=self.tmp_path)
        self.assertEqual(ctx.exception.fields["agents.3.name"], "name is required")
        self.assertEqual(ctx.exception.fields["agents.4.cmd"], "command is required")
        self.assertEqual(ctx.exception.fields["agents.5.cmd"], "command is required")
        self._assert_nothing_changed()
        self.assertEqual(self.config["harvest"]["mode"], "click")  # the valid field didn't sneak through

    def test_name_with_whitespace_is_rejected(self):
        rows = self._rows() + [{"name": "my bot", "cmd": "bot", "promptSuffix": ""}]
        with self.assertRaises(settings.ValidationError) as ctx:
            settings.apply_settings(self.config, {"agents": rows}, path=self.tmp_path)
        self.assertIn("agents.3.name", ctx.exception.fields)

    def test_duplicate_names_are_rejected_case_insensitively(self):
        rows = self._rows() + [{"name": "Codex", "cmd": "codex", "promptSuffix": ""}]
        with self.assertRaises(settings.ValidationError) as ctx:
            settings.apply_settings(self.config, {"agents": rows}, path=self.tmp_path)
        self.assertIn("duplicates agent 'codex'", ctx.exception.fields["agents.3.name"])

    def test_unbalanced_quotes_in_command_are_rejected_with_the_reason(self):
        rows = self._rows()
        rows[2]["cmd"] = "claude --append-system-prompt 'oops"
        with self.assertRaises(settings.ValidationError) as ctx:
            settings.apply_settings(self.config, {"agents": rows}, path=self.tmp_path)
        self.assertIn("could not parse", ctx.exception.fields["agents.2.cmd"])
        self._assert_nothing_changed()

    def test_non_string_suffix_and_non_object_row_are_rejected(self):
        rows = self._rows() + ["bot", {"name": "b", "cmd": "b", "promptSuffix": 5}]
        with self.assertRaises(settings.ValidationError) as ctx:
            settings.apply_settings(self.config, {"agents": rows}, path=self.tmp_path)
        self.assertIn("agents.3", ctx.exception.fields)
        self.assertIn("agents.4.promptSuffix", ctx.exception.fields)

    def test_agents_that_is_not_a_list_is_a_400_settings_error(self):
        with self.assertRaises(settings.SettingsError):
            settings.apply_settings(self.config, {"agents": {"claude": ["claude"]}}, path=self.tmp_path)
        self._assert_nothing_changed()

    def test_dropping_a_builtin_is_refused_editing_it_is_not(self):
        rows = [r for r in self._rows() if r["name"] != "codex"]
        with self.assertRaises(settings.ValidationError) as ctx:
            settings.apply_settings(self.config, {"agents": rows}, path=self.tmp_path)
        self.assertIn("cannot be removed", ctx.exception.fields["agents.codex"])
        self._assert_nothing_changed()

    def test_empty_map_is_refused(self):
        with open(self.tmp_path, "w") as f:
            json.dump({"projects": [{"name": "my-app", "path": "~/x/my-app"}], "agents": {"bot": ["bot"]}}, f)
        config = server.load_config(path=self.tmp_path)
        with self.assertRaises(settings.ValidationError) as ctx:
            settings.apply_settings(config, {"agents": [], "defaultAgent": "bot"}, path=self.tmp_path)
        self.assertIn("agents", ctx.exception.fields)

    # -- default agent interplay -------------------------------------

    def test_removing_the_default_agent_without_a_replacement_is_refused(self):
        settings.apply_settings(self.config, {"defaultAgent": "reviewer"}, path=self.tmp_path)
        rows = [r for r in self._rows() if r["name"] != "reviewer"]
        with self.assertRaises(settings.ValidationError) as ctx:
            settings.apply_settings(self.config, {"agents": rows}, path=self.tmp_path)
        self.assertIn("'reviewer' is the default agent", ctx.exception.fields["defaultAgent"])
        self.assertIn("reviewer", self.config["agents"])
        self.assertEqual(self.config["defaultAgent"], "reviewer")

    def test_removing_the_default_agent_with_a_replacement_in_the_same_save_works(self):
        settings.apply_settings(self.config, {"defaultAgent": "reviewer"}, path=self.tmp_path)
        rows = [r for r in self._rows() if r["name"] != "reviewer"]
        result = settings.apply_settings(self.config, {"agents": rows, "defaultAgent": "codex"}, path=self.tmp_path)
        self.assertEqual(result["defaultAgent"], "codex")
        self.assertNotIn("reviewer", self._disk()["agents"])
        self.assertEqual(self._disk()["defaultAgent"], "codex")

    def test_default_agent_may_name_an_agent_added_in_the_same_save(self):
        rows = self._rows() + [{"name": "bot", "cmd": "bot", "promptSuffix": ""}]
        result = settings.apply_settings(self.config, {"agents": rows, "defaultAgent": "bot"}, path=self.tmp_path)
        self.assertEqual(result["defaultAgent"], "bot")
        self.assertEqual(self.config["defaultAgent"], "bot")

    # -- PATH warning: warn, never block -----------------------------

    def test_executable_not_on_path_warns_but_saves(self):
        rows = self._rows() + [{"name": "bot", "cmd": "botx --flag", "promptSuffix": ""}]
        with mock.patch.object(server, "which", side_effect=lambda name: None if name == "botx" else "/usr/bin/x"):
            result = settings.apply_settings(self.config, {"agents": rows}, path=self.tmp_path)
        self.assertEqual(len(result["warnings"]), 1)
        self.assertIn("agent 'bot'", result["warnings"][0])
        self.assertIn("'botx' was not found on PATH", result["warnings"][0])
        self.assertEqual(self._disk()["agents"]["bot"], ["botx", "--flag"])  # saved anyway
        entries = {e["name"]: e for e in result["agentEntries"]}
        self.assertFalse(entries["bot"]["onPath"])
        self.assertTrue(entries["claude"]["onPath"])

    def test_path_check_expands_a_home_relative_wrapper(self):
        seen = []
        with mock.patch.object(server, "which", side_effect=lambda name: seen.append(name) or "/x"):
            settings.apply_settings(
                self.config, {"agents": self._rows() + [{"name": "w", "cmd": "~/bin/agent.sh", "promptSuffix": ""}]},
                path=self.tmp_path,
            )
        self.assertIn(os.path.expanduser("~/bin/agent.sh"), seen)

    # -- live application --------------------------------------------

    def test_next_spawn_resolves_an_agent_added_by_the_editor(self):
        import spawn
        project = self.config["projects"][0]
        rows = self._rows() + [{"name": "newbot", "cmd": "newbot --fast", "promptSuffix": "Go."}]
        with mock.patch.object(server, "run_backlog", return_value=_task_view_proc(["@newbot"])):
            before = spawn.resolve_agent(self.config, project, "TASK-2")
            settings.apply_settings(self.config, {"agents": rows}, path=self.tmp_path)
            after = spawn.resolve_agent(self.config, project, "TASK-2")
        self.assertEqual(before[0], "claude")  # unknown assignee -> default, before the save
        self.assertEqual(after, ("newbot", ["newbot", "--fast"], "Go."))

    def test_next_spawn_no_longer_resolves_a_deleted_agent(self):
        import spawn
        project = self.config["projects"][0]
        rows = [r for r in self._rows() if r["name"] != "reviewer"]
        with mock.patch.object(server, "run_backlog", return_value=_task_view_proc(["@reviewer"])):
            self.assertEqual(spawn.resolve_agent(self.config, project, "TASK-2")[0], "reviewer")
            settings.apply_settings(self.config, {"agents": rows}, path=self.tmp_path)
            self.assertEqual(spawn.resolve_agent(self.config, project, "TASK-2")[0], "claude")

    def test_edited_builtin_command_is_what_the_next_spawn_launches(self):
        import spawn
        project = self.config["projects"][0]
        rows = self._rows()
        rows[0]["cmd"] = "claude --model opus"
        with mock.patch.object(server, "run_backlog", return_value=_task_view_proc(["@claude"])):
            settings.apply_settings(self.config, {"agents": rows}, path=self.tmp_path)
            self.assertEqual(spawn.resolve_agent(self.config, project, "TASK-2")[1], ["claude", "--model", "opus"])


class AgentsEditorHttpTests(_SettingsHttpBase):
    """The same editor flow over the real HTTP handler."""

    def test_get_includes_agent_entries(self):
        with mock.patch.object(server, "which", return_value="/usr/bin/x"):
            status, body = self._get("/api/settings")
        self.assertEqual(status, 200)
        self.assertEqual([e["name"] for e in body["agentEntries"]], ["claude", "codex"])
        self.assertTrue(all(e["builtin"] for e in body["agentEntries"]))

    def test_post_agents_round_trip_add_then_delete(self):
        self.config["agents"] = {"claude": {"cmd": ["claude"]}, "codex": {"cmd": ["codex"]}}
        rows = [
            {"name": "claude", "cmd": "claude", "promptSuffix": ""},
            {"name": "codex", "cmd": "codex", "promptSuffix": ""},
            {"name": "bot", "cmd": "bot --append-system-prompt 'be kind'", "promptSuffix": "Suffix."},
        ]
        with mock.patch.object(server, "which", side_effect=lambda name: None if name == "bot" else "/usr/bin/x"):
            status, body = self._post("/api/settings", json.dumps({"agents": rows, "defaultAgent": "bot"}).encode())
        self.assertEqual(status, 200, body)
        self.assertEqual(body["defaultAgent"], "bot")
        entry = [e for e in body["agentEntries"] if e["name"] == "bot"][0]
        self.assertEqual(entry["cmd"], ["bot", "--append-system-prompt", "be kind"])
        self.assertEqual(entry["cmdText"], "bot --append-system-prompt 'be kind'")
        self.assertFalse(entry["onPath"])
        self.assertEqual(len(body["warnings"]), 1)
        with open(self.tmp_path) as f:
            disk = json.load(f)
        self.assertEqual(disk["agents"]["bot"], {"cmd": ["bot", "--append-system-prompt", "be kind"], "promptSuffix": "Suffix."})

        with mock.patch.object(server, "which", return_value="/usr/bin/x"):
            status, body = self._post("/api/settings", json.dumps({"agents": rows[:2], "defaultAgent": "claude"}).encode())
        self.assertEqual(status, 200, body)
        self.assertEqual([e["name"] for e in body["agentEntries"]], ["claude", "codex"])
        with open(self.tmp_path) as f:
            self.assertNotIn("bot", json.load(f)["agents"])

    def test_post_agents_validation_errors_are_indexed_fields(self):
        rows = [
            {"name": "claude", "cmd": "claude", "promptSuffix": ""},
            {"name": "codex", "cmd": "codex", "promptSuffix": ""},
            {"name": "", "cmd": "", "promptSuffix": ""},
        ]
        status, body = self._post("/api/settings", json.dumps({"agents": rows}).encode())
        self.assertEqual(status, 400)
        self.assertEqual(body["fields"]["agents.2.name"], "name is required")
        self.assertEqual(body["fields"]["agents.2.cmd"], "command is required")
