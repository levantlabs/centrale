"""Tests for scripts/scan_release.py (task-109).

Every seeded secret in this file is BUILT AT RUNTIME by concatenation
rather than written as a literal. That is not decoration: the last test
here runs the scanner over this very repository and asserts it is clean,
so a test file full of literal AKIA keys and home paths would fail its
own suite. Splitting each string keeps the fixture honest -- the scanner
sees the assembled value, the repository never contains it.
"""

import contextlib
import importlib.util
import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "scan_release.py"

# scripts/ is not a package (and must not become one -- release.sh runs
# the file by path), so load it the way a path-run script is loaded.
_spec = importlib.util.spec_from_file_location("scan_release", SCRIPT)
scan_release = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(scan_release)


class ScanTestCase(unittest.TestCase):
    """A throwaway snapshot directory plus a fixed set of rules, so no
    test depends on the identity of the machine running it."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.root = Path(self.tmpdir.name)

    def write(self, relpath, content):
        path = self.root / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content if isinstance(content, bytes)
                         else content.encode("utf-8"))
        return relpath

    def findings(self, private_names=()):
        """Scan the temp tree with an explicit rule set: the machine's own
        derived identity is deliberately left out, so every assertion here
        means the same thing on every machine."""
        rules = list(scan_release.CREDENTIAL_RULES) + list(scan_release.IDENTITY_RULES)
        rules.extend(scan_release.private_name_rules(list(private_names)))
        return scan_release.scan(self.root, rules,
                                 scan_release.walked_files(self.root))

    def rule_names(self, findings):
        return sorted({f.rule.name for f in findings})


class CleanSnapshotTests(ScanTestCase):
    def test_ordinary_content_produces_no_findings(self):
        self.write("README.md", "# A project\n\nRun `python3 server.py`.\n")
        self.write("app.py", "def add(a, b):\n    return a + b\n")
        self.assertEqual(self.findings(), [])

    def test_binary_files_are_skipped_rather_than_decoded(self):
        secret = "AKIA" + "B" * 16
        self.write("docs/img/shot.png", b"\x89PNG\r\n\x1a\n\x00\x00" + secret.encode())
        self.assertEqual(self.findings(), [])

    def test_a_field_name_without_a_value_is_not_a_secret(self):
        # The distinction the whole rule rests on: naming the field is
        # documentation, carrying its value is a leak.
        self.write("docs/api.md", '`password` is required.\n')
        self.write("conf.py", 'api_key = os.environ["API_KEY"]\n')
        self.write("schema.json", '{"secret": null, "password": ""}\n')
        self.assertEqual(self.findings(), [])


class CredentialTests(ScanTestCase):
    def test_aws_access_key_id_fails(self):
        self.write("conf.py", 'AWS = "' + "AKIA" + "Q" * 16 + '"\n')
        self.assertEqual(self.rule_names(self.findings()), ["aws-access-key-id"])

    def test_private_key_header_fails(self):
        self.write("id_rsa", "-----BEGIN RSA PRIVATE" + " KEY-----\nMIIE...\n")
        self.assertEqual(self.rule_names(self.findings()), ["private-key"])

    def test_github_and_openai_prefixes_fail(self):
        self.write("notes.md", "ghp" + "_" + "a" * 36 + "\n" + "sk" + "-" + "b" * 32 + "\n")
        self.assertEqual(self.rule_names(self.findings()),
                         ["github-token", "openai-key"])

    def test_bearer_header_with_a_real_token_fails(self):
        self.write("curl.sh", "curl -H 'Authorization: Bearer " + "z" * 24 + "'\n")
        self.assertEqual(self.rule_names(self.findings()), ["bearer-token"])

    def test_slack_token_fails(self):
        self.write("hook.py", 'TOKEN = "' + "xoxb" + "-" + "1" * 20 + '"\n')
        self.assertEqual(self.rule_names(self.findings()), ["slack-token"])

    def test_assignment_carrying_a_value_fails(self):
        self.write("conf.py", 'api_key = "' + "hunter2hunter2" + '"\n')
        self.write("conf.json", '{"password": "' + "correcthorse" + '"}\n')
        self.assertEqual(self.rule_names(self.findings()), ["secret-assignment"])
        self.assertEqual(len(self.findings()), 2)

    def test_credential_rules_still_run_over_identity_exempt_paths(self):
        # backlog/ is exempt from the IDENTITY rules only. A key must
        # never ship from anywhere, exemption or not.
        self.write("backlog/tasks/task-1 - Something.md",
                   "key: " + "AKIA" + "R" * 16 + "\n")
        self.assertEqual(self.rule_names(self.findings()), ["aws-access-key-id"])


class IdentityTests(ScanTestCase):
    def test_a_real_home_path_fails(self):
        self.write("docs/operations.md", "cd /" + "home/" + "someone" + "/code\n")
        self.assertEqual(self.rule_names(self.findings()), ["home-path"])

    def test_a_macos_home_path_fails(self):
        self.write("README.md", "/" + "Users/" + "someone" + "/code/app\n")
        self.assertEqual(self.rule_names(self.findings()), ["home-path"])

    def test_the_generic_placeholder_home_path_passes(self):
        # The allowlisted literal from the API examples -- names no account.
        self.write("docs/api.md", '{"path": "/home/user/code/my-app"}\n')
        self.assertEqual(self.findings(), [])

    def test_a_real_looking_email_fails(self):
        self.write("AUTHORS", "someone" + "@" + "realdomain.co\n")
        self.assertEqual(self.rule_names(self.findings()), ["email-address"])

    def test_reserved_example_domains_and_ssh_urls_pass(self):
        self.write("scripts/x.sh",
                   'REMOTE="git@github.com:you/repo.git"\n'
                   'EMAIL="noreply@example.com"\n'
                   'OTHER="itest@example.org"\n')
        self.assertEqual(self.findings(), [])

    def test_identity_rules_stop_at_the_exempt_paths(self):
        line = "cd /" + "home/" + "someone" + "/code\n"
        self.write("backlog/tasks/task-1 - Something.md", line)
        self.assertEqual(self.findings(), [])
        # ...and the exemption is a prefix on the path, not a substring:
        # a file merely *named* backlog elsewhere is still scanned.
        self.write("docs/backlog/notes.md", line)
        self.assertEqual(self.rule_names(self.findings()), ["home-path"])


class PrivateProjectNameTests(ScanTestCase):
    def test_a_configured_name_fails_wherever_it_appears(self):
        self.write("tests/test_x.py", '{"project": "acmeinternal"}\n')
        self.assertEqual(self.rule_names(self.findings(["acmeinternal"])),
                         ["private-project-name"])

    def test_the_match_is_case_insensitive_and_bounded(self):
        self.write("a.md", "AcmeInternal\n")
        self.write("b.md", "acmeinternally\n")   # a longer word, not the name
        findings = self.findings(["acmeinternal"])
        self.assertEqual([f.path for f in findings], ["a.md"])

    def test_no_configured_names_means_no_name_rules(self):
        self.write("tests/test_x.py", '{"project": "acmeinternal"}\n')
        self.assertEqual(self.findings(), [])


class PrivateNameConfigTests(unittest.TestCase):
    """RELEASE_PRIVATE_NAMES resolution: the environment first, then the
    gitignored .release-remote the rest of the release config lives in."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.root = Path(self.tmpdir.name)
        # Never let the developer's own .release-remote reach a test.
        patcher = mock.patch.object(
            scan_release, "REPO_ROOT", self.root / "nonexistent")
        patcher.start()
        self.addCleanup(patcher.stop)

    def _config(self, text):
        (self.root / scan_release.CONFIG_BASENAME).write_text(text, encoding="utf-8")

    def test_names_come_from_the_release_config_file(self):
        self._config('RELEASE_REMOTE="git@github.com:you/r.git"\n'
                     'RELEASE_PRIVATE_NAMES="acme skunkworks"\n')
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("RELEASE_PRIVATE_NAMES", None)
            names, source = scan_release.load_private_names(self.root)
        self.assertEqual(names, ["acme", "skunkworks"])
        self.assertIn(scan_release.CONFIG_BASENAME, source)

    def test_the_environment_wins_over_the_file(self):
        self._config('RELEASE_PRIVATE_NAMES="fromfile"\n')
        with mock.patch.dict(os.environ,
                                      {"RELEASE_PRIVATE_NAMES": "fromenv"}):
            names, source = scan_release.load_private_names(self.root)
        self.assertEqual(names, ["fromenv"])
        self.assertIn("environment", source)

    def test_commas_single_quotes_and_export_are_all_accepted(self):
        self._config("export RELEASE_PRIVATE_NAMES='acme, skunkworks'\n")
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("RELEASE_PRIVATE_NAMES", None)
            names, _source = scan_release.load_private_names(self.root)
        self.assertEqual(names, ["acme", "skunkworks"])

    def test_no_config_and_no_environment_means_no_names(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("RELEASE_PRIVATE_NAMES", None)
            names, source = scan_release.load_private_names(self.root)
        self.assertEqual(names, [])
        # No source is what "unarmed" is made of: main() turns exactly
        # this into exit 3 rather than a clean verdict (task-166).
        self.assertIsNone(source)

    def test_a_declared_empty_list_is_armed_rather_than_missing(self):
        """"There are no private names here" is an answer. It has to be
        distinguishable from having asked nobody, because the second is
        what silently passed in every spawn worktree before task-166."""
        self._config('RELEASE_PRIVATE_NAMES=""\n')
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("RELEASE_PRIVATE_NAMES", None)
            names, source = scan_release.load_private_names(self.root)
        self.assertEqual(names, [])
        self.assertIsNotNone(source)

    def test_an_empty_environment_value_is_a_declaration_too(self):
        with mock.patch.dict(os.environ, {"RELEASE_PRIVATE_NAMES": ""}):
            names, source = scan_release.load_private_names(self.root)
        self.assertEqual(names, [])
        self.assertIn("environment", source)


class LinkedWorktreeConfigTests(unittest.TestCase):
    """task-166: the same tracked content, scanned from the main checkout
    and from a linked worktree of it, must reach the same verdict.

    .release-remote is gitignored -- which is exactly what keeps it out
    of every published snapshot -- so it exists in ONE checkout. Every
    line of code in this project is written in a `git worktree add` spawn
    worktree, which has none of its own, so before task-166 the
    private-name rules were armed only where the work has already landed.
    task-157 merged 26 private names into two shipping test files with
    the suite green in the worktree that wrote them.
    """

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        # REPO_ROOT is the scan's own repo -- the real one, whose real
        # .release-remote would arm every scan here and prove nothing.
        patcher = mock.patch.object(
            scan_release, "REPO_ROOT", Path(self.tmpdir.name) / "nonexistent")
        patcher.start()
        self.addCleanup(patcher.stop)

        env_patcher = mock.patch.dict(os.environ, {}, clear=False)
        env_patcher.start()
        self.addCleanup(env_patcher.stop)
        os.environ.pop("RELEASE_PRIVATE_NAMES", None)

        self.main = Path(self.tmpdir.name) / "main"
        self.main.mkdir()
        self._git("init", "-q", "-b", "main", ".")
        self._git("config", "user.name", "Ada Lovelace")
        self._git("config", "user.email", "release@example.invalid")
        # Tracked, and carrying the private name: the shape of the leak
        # this whole rule class exists for.
        (self.main / ".gitignore").write_text(
            ".release-remote\n.centrale-worktrees/\n", encoding="utf-8")
        (self.main / "fixture.json").write_text(
            '{"project": "acmeinternal"}\n', encoding="utf-8")
        self._git("add", "-A")
        self._git("commit", "-q", "-m", "seed")
        # ...and the gitignored config, which only this checkout has.
        (self.main / scan_release.CONFIG_BASENAME).write_text(
            'RELEASE_REMOTE="git@github.com:you/r.git"\n'
            'RELEASE_PRIVATE_NAMES="acmeinternal"\n', encoding="utf-8")

        # A spawn worktree, made the way spawn.py makes one.
        self.worktree = self.main / ".centrale-worktrees" / "fixture-task-1"
        self._git("worktree", "add", "-q", "-b", "task/1", str(self.worktree))

    def _git(self, *args):
        return subprocess.run(["git", "-C", str(self.main), *args], check=True,
                              capture_output=True, text=True, timeout=60)

    def _verdict(self, root):
        """What a full run over `root` concludes: the findings it names,
        and whether the private-name rules were armed at all."""
        rules, _lines, armed = scan_release.build_rules(root)
        files, _how = scan_release.target_files(root)
        findings = scan_release.scan(root, rules, files)
        return sorted({(f.path, f.lineno, f.rule.name) for f in findings}), armed

    def test_the_worktree_resolves_the_main_checkouts_private_names(self):
        self.assertEqual(scan_release.load_private_names(self.worktree),
                         scan_release.load_private_names(self.main))
        names, source = scan_release.load_private_names(self.worktree)
        self.assertEqual(names, ["acmeinternal"])
        self.assertEqual(source, str(self.main / scan_release.CONFIG_BASENAME))

    def test_the_two_checkouts_reach_the_same_verdict_on_the_same_content(self):
        main_verdict = self._verdict(self.main)
        worktree_verdict = self._verdict(self.worktree)
        self.assertEqual(worktree_verdict, main_verdict)
        findings, armed = worktree_verdict
        self.assertTrue(armed)
        # Not vacuously equal: both actually FAIL, on the seeded name.
        self.assertEqual([f[2] for f in findings], ["private-project-name"])

    def test_a_tree_with_no_configuration_anywhere_is_unarmed_not_clean(self):
        """The other side of the same coin: with nothing to resolve, the
        run reports that it could not look rather than that it found
        nothing -- and exits 3, which is neither clean nor a finding."""
        plain = Path(self.tmpdir.name) / "elsewhere"
        (plain / "sub").mkdir(parents=True)
        (plain / "sub" / "a.txt").write_text("nothing to see\n", encoding="utf-8")
        findings, armed = self._verdict(plain)
        self.assertEqual(findings, [])
        self.assertFalse(armed)
        printed = io.StringIO()          # the verdict is under test, not the noise
        with contextlib.redirect_stdout(printed):
            status = scan_release.main([str(plain)])
        self.assertEqual(status, scan_release.EXIT_UNARMED)
        self.assertIn("UNARMED", printed.getvalue())


class DerivedIdentityTests(unittest.TestCase):
    def test_a_generic_login_name_never_becomes_a_rule(self):
        # "user" appears in ordinary prose in every repo; turning it into
        # a rule would make the scan useless rather than strict.
        with mock.patch.object(scan_release, "getpass") as getpass_:
            getpass_.getuser.return_value = "user"
            with mock.patch.dict(os.environ, {"USER": "user"}), \
                 mock.patch.object(scan_release, "git_config",
                                            return_value=None), \
                 mock.patch.object(scan_release.Path, "home",
                                            return_value=Path("/home/user")):
                rules, used, skipped = scan_release.derived_identity_rules(Path("."))
        self.assertEqual(rules, [])
        self.assertEqual(used, [])
        self.assertIn("user", skipped)

    def test_a_real_login_name_and_git_identity_become_rules(self):
        def fake_git_config(_root, key):
            return {"user.email": "someone@" + "corp.example",
                    "user.name": "Ada Lovelace"}.get(key)

        with mock.patch.object(scan_release, "getpass") as getpass_:
            getpass_.getuser.return_value = "adalovelace"
            with mock.patch.dict(os.environ, {"USER": "adalovelace"}), \
                 mock.patch.object(scan_release, "git_config",
                                            side_effect=fake_git_config), \
                 mock.patch.object(scan_release.Path, "home",
                                            return_value=Path("/home/" + "adalovelace")):
                rules, used, _skipped = scan_release.derived_identity_rules(Path("."))
        self.assertIn("adalovelace", used)
        self.assertIn("Ada Lovelace", used)
        self.assertIn("Lovelace", used)     # the parts leak as readily
        self.assertNotIn("Ada", used)       # ...but three letters is prose
        self.assertTrue(any(r.pattern.search("hello adalovelace here") for r in rules))


class AllowlistTests(unittest.TestCase):
    def test_every_entry_names_a_real_rule_and_carries_a_reason(self):
        known = {r.name for r in scan_release.CREDENTIAL_RULES + scan_release.IDENTITY_RULES}
        known |= {"private-project-name", "machine-account", "git-email", "git-name"}
        self.assertTrue(scan_release.ALLOWLIST)
        for rule_name, pattern, reason in scan_release.ALLOWLIST:
            self.assertIn(rule_name, known, f"{rule_name} matches no rule")
            self.assertTrue(reason.strip(), f"{rule_name}: allowlisted with no reason")
            self.assertGreater(len(reason), 20, f"{rule_name}: reason too thin to audit")


class FileEnumerationTests(unittest.TestCase):
    def test_a_git_checkout_is_enumerated_from_tracked_content(self):
        # Not a directory walk: tracked content is exactly what
        # `git archive HEAD` publishes, which is also what keeps a local
        # projects.json or .release-remote -- gitignored, and full of
        # precisely what this scans for -- out of a standalone run.
        files, how = scan_release.target_files(REPO_ROOT)
        self.assertEqual(how, "tracked content")
        self.assertIn("scripts/scan_release.py", files)
        self.assertNotIn("projects.json", files)

    def test_export_ignored_paths_are_left_out_because_they_never_ship(self):
        # Tracked is a superset of published: task-101 excluded this
        # repo's own board with `backlog/ export-ignore`, which git
        # archive honours and `git ls-files` knows nothing about. The
        # scan follows what ships, so the board is not scanned here at
        # all -- see tests/test_release_snapshot.py for the assertion
        # that the exclusion itself is still wired up.
        files, _how = scan_release.target_files(REPO_ROOT)
        self.assertEqual([f for f in files if f.startswith("backlog/")], [])
        self.assertIn("README.md", files)

    def test_export_ignore_is_read_through_directories_not_only_files(self):
        # The trap this filter exists to avoid: a directory-shaped
        # pattern answers "unspecified" for a file inside it, and even
        # for the bare directory name -- git only reads it as a
        # directory when asked with the trailing slash.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "-C", tmp, "init", "-q"], check=True)
            (root / ".gitattributes").write_text("private/ export-ignore\n",
                                                 encoding="utf-8")
            (root / "private").mkdir()
            (root / "private" / "notes.md").write_text("x", encoding="utf-8")
            (root / "kept.md").write_text("x", encoding="utf-8")
            subprocess.run(["git", "-C", tmp, "add", "-A"], check=True)

            self.assertEqual(
                scan_release.export_ignored(
                    root, [".gitattributes", "kept.md", "private/notes.md"]),
                {"private/notes.md"},
            )

    def test_a_plain_directory_falls_back_to_a_walk(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "sub").mkdir()
            (root / "sub" / "a.txt").write_text("x", encoding="utf-8")
            (root / "__pycache__").mkdir()
            (root / "__pycache__" / "b.pyc").write_text("x", encoding="utf-8")
            self.assertEqual(scan_release.walked_files(root), ["sub/a.txt"])


class ThisRepositoryIsCleanTests(unittest.TestCase):
    """The regression guard. task-105 reintroduced a private repo name
    into tests/test_browser.py weeks after task-100 removed it, and
    nothing noticed until this scan was written. Running the scan in the
    suite means the next reintroduction fails at test time rather than at
    release time.

    task-157 then did it anyway -- 26 private names into two shipping
    test files -- with this class green, because it ran in a spawn
    worktree where nothing could resolve the gitignored name list, so
    every private-name rule matched nothing and "found none" and "looked
    for none" printed the same word. task-166 closed both halves: the
    scan now reaches the main checkout's config from a linked worktree,
    and a run that resolved no configuration ANYWHERE exits 3 instead of
    reporting a clean tree.

    Which of those two outcomes is acceptable here depends on the tree.
    A tree with a `backlog/` board is a development checkout -- and the
    board is where private project names come from in the first place, so
    that tree has a list to declare and must be scanned against it. The
    published snapshot never carries one (decision-3, enforced by
    .gitattributes and tests/test_release_snapshot.py), so a clone of it
    has nothing to resolve and says so; a real finding still fails there.
    """

    def has_a_board(self):
        return (REPO_ROOT / "backlog").is_dir()

    def assert_this_tree_passes(self, proc):
        """Exit 1 -- an actual finding -- fails everywhere. Exit 3 is
        only tolerated where there is no board to draw a list from."""
        output = proc.stdout + proc.stderr
        if self.has_a_board():
            self.assertEqual(
                proc.returncode, 0,
                "the scan did not report a clean, ARMED run over this "
                "development checkout:\n" + output)
            return
        self.assertIn(proc.returncode, (0, scan_release.EXIT_UNARMED), output)
        if proc.returncode == scan_release.EXIT_UNARMED:
            self.assertIn("UNARMED", output)

    def test_the_tracked_tree_carries_no_secret_or_personal_identity(self):
        # The environment is deliberately NOT scrubbed of
        # RELEASE_PRIVATE_NAMES: the tree has to be checked under the
        # configuration actually in force, which is the whole of what
        # task-166 was about. In a checkout with no such variable set --
        # every spawn worktree, and the machine this is usually run on --
        # that means the file, resolved through the main checkout.
        proc = subprocess.run(
            [sys.executable, str(SCRIPT), str(REPO_ROOT)],
            capture_output=True, text=True, timeout=300,
        )
        self.assert_this_tree_passes(proc)

    def test_the_script_runs_standalone_with_no_arguments(self):
        # A contributor runs the same check the release runs, from the
        # repo root, with nothing installed.
        proc = subprocess.run(
            [sys.executable, "scripts/scan_release.py"],
            cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=300,
        )
        self.assert_this_tree_passes(proc)

    def test_the_private_name_rules_are_armed_in_this_checkout(self):
        """task-166's own regression test, in the environment where it
        went wrong: no RELEASE_PRIVATE_NAMES in the environment, and a
        list that has to be found on disk anyway. In a linked worktree
        that resolution crosses into the main checkout; if it ever stops
        doing so, this fails here rather than at the next release."""
        if not self.has_a_board():
            self.skipTest("no backlog/ board in this tree, so there is no "
                          "list of private project names to resolve")
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("RELEASE_PRIVATE_NAMES", None)
            _names, source = scan_release.load_private_names(REPO_ROOT)
        self.assertIsNotNone(
            source,
            "no %s could be resolved for this checkout, so the private-name "
            "rules would match nothing -- looked in: %s"
            % (scan_release.CONFIG_BASENAME,
               ", ".join(str(d) for d in scan_release.config_search_path(REPO_ROOT))))


if __name__ == "__main__":
    unittest.main()
