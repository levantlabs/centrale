"""The one property scripts/screenshots.py cannot be allowed to lose.

The images under docs/img/ are published, and scripts/scan_release.py --
the gate that reads every shipped file for a secret or a personal name --
reads TEXT. A screenshot of the maintainer's real board would sail
straight through it. So the screenshot script's guarantee is not "it is
careful": it builds a throwaway sandbox and every faked CLI boundary
REFUSES a working directory outside it. That refusal is what these tests
pin, together with the absence of any flag that would offer the
convenience back.

Deliberately hermetic and Playwright-free: nothing here launches a
browser, binds a port or runs the script. Playwright and Pillow are
maintainer-only tools (see docs/operations.md, "Regenerating the
documentation screenshots"), and a test that needed either would be the
third release prerequisite that whole design exists to avoid -- so
importing the module must keep working with neither installed, which is
itself one of the assertions below.
"""

import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "screenshots.py"
DOCS_IMG = REPO_ROOT / "docs" / "img"

# scripts/ is not a package (release.sh runs its files by path), so this
# loads the module the same way tests/test_scan_release.py loads the scan.
_spec = importlib.util.spec_from_file_location("centrale_screenshots", SCRIPT)
screenshots = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(screenshots)


def _fixture():
    with open(screenshots.FIXTURE_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


class SandboxRefusalTests(unittest.TestCase):
    """Every faked boundary refuses a cwd outside the sandbox."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.world = screenshots.World(_fixture(), self.tmpdir.name)
        self.world.build()

    def test_a_backlog_call_outside_the_sandbox_is_refused_and_recorded(self):
        with self.assertRaises(screenshots.Refusal):
            self.world.run_backlog(["task", "list", "--json"], str(REPO_ROOT))
        self.assertEqual(len(self.world.refusals), 1)
        self.assertIn("task list", self.world.refusals[0])

    def test_a_git_call_outside_the_sandbox_is_refused(self):
        with self.assertRaises(screenshots.Refusal):
            self.world.run_git(["status", "--porcelain"], str(Path.home()))

    def test_a_git_call_with_no_cwd_at_all_is_refused(self):
        # cwd=None runs in whatever directory the process happens to be
        # in -- the repo itself, when the script is run the documented
        # way. "No directory" must read as "outside the sandbox", not as
        # a hole in the guard.
        with self.assertRaises(screenshots.Refusal):
            self.world.run_git(["--version"], None)

    def test_the_same_calls_inside_the_sandbox_are_answered(self):
        project = self.world.projects[0]
        data = self.world.run_backlog(
            ["task", "list", "--json"], self.world.repo_path(project["name"]))
        self.assertEqual(data["schemaVersion"], 1)
        self.assertEqual(len(data["tasks"]), len(project["tasks"]))
        proc = self.world.run_git(
            ["for-each-ref", "--format=%(refname:short)", "refs/heads/task"],
            self.world.repo_path(project["name"]))
        self.assertEqual(
            proc.stdout.split(), list(project.get("taskBranches") or []))
        self.assertEqual(self.world.refusals, [])

    def test_every_path_the_config_hands_the_server_is_inside_the_sandbox(self):
        config = self.world.config()
        root = os.path.realpath(self.tmpdir.name)
        paths = [config["worktreeRoot"]] + [p["path"] for p in config["projects"]]
        for path in paths:
            self.assertTrue(
                os.path.realpath(path).startswith(root + os.sep),
                f"{path} is not inside the screenshot sandbox")


class NoWayToAimItAtARealBoardTests(unittest.TestCase):
    def test_no_command_line_option_takes_a_config_or_a_repository(self):
        """The failure mode this whole design exists to prevent is a
        helpful "--config ~/projects.json" arriving later. These four
        options are the whole surface, so a fifth is a decision someone
        has to make on purpose -- here, in this list -- rather than a
        convenience that slips in beside the others."""
        options = set()
        for action in screenshots.build_parser()._actions:
            options.update(o for o in action.option_strings if o.startswith("--"))
        options.discard("--help")
        self.assertEqual(options, {"--out", "--only", "--keep", "--verbose"})


class FixtureCoversTheCommittedImagesTests(unittest.TestCase):
    def test_one_shot_per_committed_image_and_nothing_else(self):
        fixture = _fixture()
        shot_names = sorted(s["name"] for s in fixture["shots"])
        committed = sorted(p.stem for p in DOCS_IMG.glob("*.png"))
        self.assertEqual(
            shot_names, committed,
            "scripts/screenshots_fixture.json's shots and docs/img/*.png have "
            "drifted apart: the script must regenerate every committed image "
            "and produce no orphans.")

    def test_both_themes_are_shot_explicitly(self):
        themes = {s["theme"] for s in _fixture()["shots"]}
        self.assertEqual(themes, {"light", "dark"})

    def test_the_fixture_names_no_real_project(self):
        """A cheap standing check that the invented world stayed
        invented: the two project names here are the same ones
        projects.example.json and the docs already use."""
        names = {p["name"] for p in _fixture()["projects"]}
        self.assertEqual(names, {"my-app", "my-lib"})


if __name__ == "__main__":
    unittest.main()
