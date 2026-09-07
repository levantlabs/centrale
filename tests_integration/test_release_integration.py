"""Integration coverage for scripts/release.sh (task-102), against real
git repositories and real local "scratch remotes" -- the only tier where
this script can be exercised at all.

Why not tests/ -- the hermetic tier -- for any of this: release.sh's own
release gate runs `python3 -m unittest discover tests` inside the staged
snapshot. A release.sh test living in tests/ would therefore be run BY a
release, from inside the snapshot, and would go on to build releases of
its own; a fixture repo of some kind is the only thing that can safely
drive this script, and driving it means real `git`, real clones, and a
real push. That is precisely this tier.

Nothing here contacts the network: every "remote" is a `git init --bare`
directory inside the test's own temp dir, and TMPDIR is redirected there
too, so the script's staging directories land under the test's tree
rather than the machine's /tmp. Nothing here touches the Centrale repo
itself -- the fixture is a tiny throwaway project (a `server.py` that
answers `--check`, a one-test `tests/` package) with a copy of the real
scripts/release.sh AND the real scripts/scan_release.py in it, which is
exactly how both are meant to work: nothing in either is specific to
Centrale. The scan is copied rather than stubbed because the gate runs
the snapshot's OWN copy of it, and a stub would prove nothing about the
check a release actually gets.

What is covered is the release-blocker class of behaviour, the failures
that would be unrecoverable or silent in production:

- a remote carrying a commit under any identity other than the release
  identity is refused before anything is staged (GitHub's "Add a README
  file", checked by default, is authored under the maintainer's real name
  and email -- and this script never uses --force, so a release built on
  top of it could never be walked back);
- a flag written AFTER the message is still a flag, so a misordered
  `--dry-run` cannot perform a real push;
- environment variables beat .release-remote, as the usage text and
  docs/operations.md both promise;
- a gate failure pushes nothing and leaves the staged snapshot on disk;
- a snapshot carrying something that looks like a secret or a personal
  identity is refused BEFORE the suite runs, and a snapshot with no scan
  in it at all is a failure rather than a silent skip (task-109);
- a scan nothing armed is refused too (task-166): with no
  RELEASE_PRIVATE_NAMES set, no private-name rule can fire, and a check
  that ran none of its rules must not read as a clean snapshot -- while
  an explicitly EMPTY list is a declaration that there are none, and
  releases;
- a missing tool on the release machine is reported as such (exit 3),
  distinctly from a snapshot that failed the gate (exit 1) -- including
  `node`, without which the frontend behavioural tier does not fail but
  SKIPS, and the gate passes on a suite that executed no JavaScript at
  all (task-124);
- a test tier that skips itself for want of a runtime is a gate failure
  under a release, because the gate runs the snapshot's suite with
  `CENTRALE_REQUIRE_NODE=1` -- while the same tier skips harmlessly, and
  the suite still reports OK, outside one;
- the versioning half (task-107): a release is tagged `v<version>` from
  the snapshot's own constant, annotated, under the release identity --
  and a snapshot whose content changed while the version did not is
  refused, while unchanged content stays the benign "nothing to
  release"; a bump with no CHANGELOG section fails the gate;
- the integration tier is part of the gate (task-130): it runs against
  the STAGED tree after the unit tier, with CENTRALE_REQUIRE_INTEGRATION=1
  so a test that would skip for want of a tool fails and names it; a
  failing integration test pushes nothing; a snapshot with no
  tests_integration/ in it fails rather than skips; and `tmux` is a
  release-machine prerequisite (exit 3) for the same reason `node` is;
- the tested `backlog` baseline (task-156): a machine whose CLI is not
  the version the snapshot declares cannot cut a release, reported as a
  machine problem (exit 3) and leaving no staging tree behind, since
  nothing in the snapshot is wrong -- the same tree releases as soon as
  the two numbers agree; a snapshot declaring no baseline at all fails
  as a snapshot (exit 1), because a release untied to a CLI is what the
  constant exists to prevent;
- the happy path: a first release, a second release on top of it, both
  under the configured identity only.

A note on recursion: the fixture project ships a tests_integration/ of
its own (a real release's gate runs the snapshot's tier, and this module
IS part of Centrale's snapshot), so when Centrale's gate runs this module
from the staged tree, each fixture release below runs the fixture's
one-test tier, not this module again.

Run explicitly: python3 -m unittest tests_integration.test_release_integration
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import unittest

from tests_integration import base

RELEASE_SH = os.path.join(base.CENTRALE_ROOT, "scripts", "release.sh")
SCAN_RELEASE_PY = os.path.join(base.CENTRALE_ROOT, "scripts", "scan_release.py")


def tearDownModule():
    # Nothing here starts a tmux session, but every module in this tier
    # asserts this run's tmux footprint -- server and socket file -- is
    # gone, so the guarantee holds regardless of which modules a
    # `discover` run happened to include.
    base.assert_test_tmux_footprint_gone()

# The release identity every fixture release is made under, and the real
# personal identity the fixture repo's own private commits use. They must
# differ: the whole point of the guard is telling them apart.
REL_NAME = "Centrale Release Bot"
REL_EMAIL = "release-itest@example.invalid"
PRIVATE_NAME = "Fixture Maintainer"
PRIVATE_EMAIL = "maintainer-itest@example.invalid"

# A `server.py` that satisfies `python3 server.py --check`, so the fixture
# can exercise the real release gate without deploying Centrale itself.
FIXTURE_SERVER_PY = """\
import sys

if "--check" in sys.argv[1:]:
    print("[PASS] fixture doctor: nothing to check")
    sys.exit(0)
raise SystemExit("fixture server: nothing to serve")
"""

# task-107: every release derives its tag from the snapshot's own
# version constant and refuses to publish a version whose tag is already
# out, so a fixture project needs the same two files a real one does --
# and the "bump before releasing again" step is part of what is tested.
#
# task-156 added the second constant: a release also refuses to run on a
# machine whose `backlog` is not the version the snapshot declares it was
# tested against. A fixture that hardcoded a baseline would therefore
# pass or fail with whatever CLI happened to be installed, so the happy
# path declares THIS machine's version -- which is exactly what a real
# project's constant claims about the machine its releases are cut on.
FIXTURE_VERSION_PY_TEMPLATE = '__version__ = "%s"\nTESTED_BACKLOG_VERSION = "%s"\n'


def machine_backlog_version():
    """The `backlog` version on this machine, read the way release.sh
    and server.backlog_version() both read it: the first three-component
    number anywhere in the output. None if there is no such number."""
    proc = base.run(["backlog", "--version"], check=False)
    match = re.search(r"(?<![\d.])(\d+\.\d+\.\d+)(?![\d.])",
                      (proc.stdout or "") + " " + (proc.stderr or ""))
    return match.group(1) if match else None

FIXTURE_CHANGELOG_MD = """\
# Changelog

## v0.1.0

- the fixture's first release
"""

PASSING_TEST_PY = """\
import unittest


class FixtureSuite(unittest.TestCase):
    def test_passes(self):
        self.assertTrue(True)
"""

# task-124: a stand-in for Centrale's frontend behavioural tier -- a test
# that needs a runtime to say anything, skips cleanly without it, and
# refuses to skip when a caller has declared a skip unacceptable. Exactly
# the shape of `tests/js_harness.requires_node`, spelled out here rather
# than imported, because the fixture project must stay a project of its
# own (nothing in release.sh is specific to Centrale). The runtime it
# looks for is named by the environment so a test can make it genuinely
# absent without hiding anything real from PATH.
RUNTIME_TIER_TEST_PY = """\
import os
import shutil
import unittest


class FixtureBehaviourTier(unittest.TestCase):
    def test_the_tier_needs_its_runtime(self):
        runtime = os.environ.get("FIXTURE_TIER_RUNTIME", "node")
        if shutil.which(runtime) is None:
            if os.environ.get("CENTRALE_REQUIRE_NODE", "").strip() not in ("", "0"):
                self.fail("the behavioural tier has no %s to run under" % runtime)
            self.skipTest("%s not available" % runtime)
        self.assertTrue(True)
"""

FAILING_TEST_PY = """\
import unittest


class FixtureSuite(unittest.TestCase):
    def test_fails(self):
        self.assertEqual(1, 2, "deliberate release-gate failure")
"""

# task-130: the fixture's own integration tier. The gate runs the
# snapshot's tests_integration/ after its tests/, so a fixture project
# needs one for the same reason it needs a tests/ -- and this one leaves
# evidence: when FIXTURE_ITEST_MARKER names a file, the test records
# where it ran from and whether the gate's strict switch reached it,
# which is how a test below proves the tier ran, ran against the STAGED
# tree rather than the checkout, and ran under the switch.
PASSING_ITEST_PY = """\
import os
import unittest


class FixtureIntegrationTier(unittest.TestCase):
    def test_runs(self):
        marker = os.environ.get("FIXTURE_ITEST_MARKER")
        if marker:
            with open(marker, "w", encoding="utf-8") as f:
                f.write(os.path.dirname(os.path.abspath(__file__)) + "\\n")
                f.write(os.environ.get("CENTRALE_REQUIRE_INTEGRATION", "") + "\\n")
        self.assertTrue(True)
"""

FAILING_ITEST_PY = """\
import unittest


class FixtureIntegrationTier(unittest.TestCase):
    def test_fails(self):
        self.assertEqual(1, 2, "deliberate integration-tier failure")
"""

# The integration-tier counterpart of RUNTIME_TIER_TEST_PY: a test that
# needs a tool, skips cleanly without it, and refuses to skip when the
# gate has declared a skip unacceptable -- the shape of
# `tests_integration.base.require_tools`, spelled out here rather than
# imported, because the fixture must stay a project of its own.
TOOL_TIER_ITEST_PY = """\
import os
import shutil
import unittest


class FixtureToolTier(unittest.TestCase):
    def test_the_tier_needs_its_tool(self):
        tool = os.environ.get("FIXTURE_ITEST_TOOL", "tmux")
        if shutil.which(tool) is None:
            if os.environ.get("CENTRALE_REQUIRE_INTEGRATION", "").strip() not in ("", "0"):
                self.fail("the integration tier has no %s to run with" % tool)
            self.skipTest("%s not available" % tool)
        self.assertTrue(True)
"""


# Every prerequisite of the release MACHINE is a prerequisite of this
# module too: a fixture release exits 3 without any one of them, which
# would fail every happy-path test here for a reason that is not the
# script's. `tmux` joins the list with task-130, `node` with task-124.
@base.require_tools("git", "tar", "python3", "backlog", "node", "tmux")
class ReleaseScriptIntegrationTests(base.IntegrationCase):
    """Drives the real scripts/release.sh in a throwaway repo."""

    def setUp(self):
        super().setUp()
        # The script's staging dirs and archive tarballs go here, not in
        # the machine's /tmp: this tier owns every path it creates.
        self.script_tmp = os.path.join(self.tmp_dir, "release-tmp")
        os.makedirs(self.script_tmp, exist_ok=True)
        # Not a skip: `backlog` is a required tool of this module, and a
        # `backlog` on PATH that reports no version is a release machine
        # no release can run on (release.sh exits 3 saying so). Failing
        # here names that, where skipping would hide it.
        self.machine_backlog = machine_backlog_version()
        self.assertIsNotNone(
            self.machine_backlog,
            "'backlog --version' reported no X.Y.Z version on this machine; "
            "release.sh cannot confirm the tested baseline without one")
        self.repo = self._make_fixture_repo()

    # -- fixtures --------------------------------------------------------

    def _git(self, *args, cwd=None, check=True):
        return base.run(["git", *args], cwd=cwd or self.repo, check=check)

    def _make_fixture_repo(self):
        """A minimal project with a copy of the real release.sh in it."""
        repo = os.path.join(self.tmp_dir, "fixture-project")
        os.makedirs(os.path.join(repo, "scripts"))
        os.makedirs(os.path.join(repo, "tests"))
        shutil.copy2(RELEASE_SH, os.path.join(repo, "scripts", "release.sh"))
        # The gate runs the snapshot's own copy of the scan, so the
        # fixture ships one exactly as a real project would.
        shutil.copy2(SCAN_RELEASE_PY, os.path.join(repo, "scripts", "scan_release.py"))
        self._write(repo, "server.py", FIXTURE_SERVER_PY)
        self._write(repo, "version.py", self._version_py("0.1.0"))
        self._write(repo, "CHANGELOG.md", FIXTURE_CHANGELOG_MD)
        self._write(repo, os.path.join("tests", "test_fixture.py"), PASSING_TEST_PY)
        self._write(repo, os.path.join("tests_integration", "__init__.py"), "")
        self._write(repo, os.path.join("tests_integration", "test_fixture_integration.py"),
                    PASSING_ITEST_PY)
        self._write(repo, "payload.txt", "v1\n")
        # A path that is gitignored AND tracked: `git add -A` alone would
        # drop it from the snapshot; the script uses `git add -A -f`.
        self._write(repo, ".gitignore", "ignored-but-tracked.txt\n")
        self._write(repo, "ignored-but-tracked.txt", "still tracked\n")
        base.run(["git", "init", "-q", "-b", "main", "."], cwd=repo)
        base.run(["git", "config", "user.name", PRIVATE_NAME], cwd=repo)
        base.run(["git", "config", "user.email", PRIVATE_EMAIL], cwd=repo)
        base.run(["git", "add", "-A", "-f"], cwd=repo)
        base.run(["git", "commit", "-q", "-m", "fixture v1"], cwd=repo)
        return repo

    def _path_without(self, *hidden):
        """A PATH directory carrying every executable the real PATH
        offers EXCEPT the named ones.

        Hiding a tool by trimming PATH down to /usr/bin:/bin (what the
        `backlog` test below does) cannot work for `node`, which usually
        lives right next to git and python3: the point is to remove ONE
        prerequisite while leaving every other one exactly where the
        script will look for it."""
        sandbox = os.path.join(self.tmp_dir, "path-without-" + "-".join(hidden))
        os.makedirs(sandbox, exist_ok=True)
        for directory in os.environ.get("PATH", "").split(os.pathsep):
            if not directory or not os.path.isdir(directory):
                continue
            for name in sorted(os.listdir(directory)):
                if name in hidden:
                    continue
                link = os.path.join(sandbox, name)
                if os.path.lexists(link):
                    continue  # first on PATH wins, as it would anyway
                try:
                    os.symlink(os.path.join(directory, name), link)
                except OSError:
                    pass
        return sandbox

    def _write(self, repo, rel, content):
        path = os.path.join(repo, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return path

    def _version_py(self, version, baseline=None):
        """The fixture's version.py: its own number, and the `backlog`
        baseline it claims to have been tested against -- this machine's,
        unless a test is deliberately declaring a different one."""
        return FIXTURE_VERSION_PY_TEMPLATE % (
            version, self.machine_backlog if baseline is None else baseline)

    def _bump_version(self, new_version, *, changelog=True):
        """The documented release workflow, as the fixture performs it:
        one number in version.py, a matching CHANGELOG section, both
        committed (release.sh publishes HEAD, never the working tree).
        `changelog=False` deliberately skips half of it, to exercise the
        gate that catches exactly that."""
        self._write(self.repo, "version.py", self._version_py(new_version))
        if changelog:
            self._write(self.repo, "CHANGELOG.md",
                        f"# Changelog\n\n## v{new_version}\n\n- the next one\n\n"
                        + FIXTURE_CHANGELOG_MD.split("\n", 2)[2])
        self._git("add", "-A", "-f")
        self._git("commit", "-q", "-m", f"bump to {new_version}")

    def remote_tags(self, remote):
        proc = base.run(["git", "tag", "-l"], cwd=remote, check=False)
        return proc.stdout.split() if proc.returncode == 0 else []

    def _bare_remote(self, name="public.git"):
        path = os.path.join(self.tmp_dir, name)
        base.run(["git", "init", "-q", "--bare", "-b", "main", path])
        return path

    def _seed_remote_with_foreign_commit(self, remote, *,
                                         author=(PRIVATE_NAME, PRIVATE_EMAIL),
                                         committer=None, name="seed"):
        """Puts one commit on the remote's branch under identities of the
        caller's choosing, and returns its sha.

        The default reproduces GitHub's auto-init: a single commit made
        wholly under a real personal identity. Author and committer are
        seeded separately (git tracks them separately, and a rebase or a
        web edit rewrites one without the other), so a test can pin down
        exactly which of the four fields the guard is reacting to."""
        committer = committer if committer is not None else author
        seed = os.path.join(self.tmp_dir, name)
        base.run(["git", "init", "-q", "-b", "main", seed])
        env = dict(os.environ)
        env.update({
            "GIT_AUTHOR_NAME": author[0], "GIT_AUTHOR_EMAIL": author[1],
            "GIT_COMMITTER_NAME": committer[0], "GIT_COMMITTER_EMAIL": committer[1],
        })
        base.run(["git", "commit", "-q", "--allow-empty", "-m", "Initial commit"],
                 cwd=seed, env=env)
        base.run(["git", "push", "-q", remote, "main"], cwd=seed)
        return base.run(["git", "rev-parse", "main"], cwd=seed).stdout.strip()

    def assert_refused_untouched(self, proc, remote, head_before):
        """The whole point of this guard: exit 1, nothing pushed, and
        nothing even staged -- it runs before the staging tree is built,
        so a refusal leaves no trace anywhere."""
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("REFUSING", proc.stderr)
        self.assertEqual(
            base.run(["git", "rev-parse", "main"], cwd=remote).stdout.strip(), head_before)
        self.assertEqual(len(self.remote_log(remote)), 1)
        self.assertEqual(self.staging_dirs(), [])

    # -- driving the script ---------------------------------------------

    def release(self, *args, remote=None, env=None, cwd=None):
        """Runs the fixture's release.sh and returns the CompletedProcess
        (never raising on a non-zero exit -- the exit code is usually the
        thing under test)."""
        run_env = dict(os.environ)
        run_env.update({
            "TMPDIR": self.script_tmp,
            "RELEASE_AUTHOR_NAME": REL_NAME,
            "RELEASE_AUTHOR_EMAIL": REL_EMAIL,
            # The fixture project has no private repos, and since
            # task-166 that has to be SAID: an unset list leaves the
            # gate's scan unarmed, which is a refusal of its own (the
            # test below drives exactly that). An empty value is the
            # declaration, and it also keeps a real RELEASE_PRIVATE_NAMES
            # in the maintainer's environment out of these fixtures.
            "RELEASE_PRIVATE_NAMES": "",
        })
        if remote is not None:
            run_env["RELEASE_REMOTE"] = remote
        else:
            run_env.pop("RELEASE_REMOTE", None)
        for key in ("RELEASE_BRANCH",):
            run_env.pop(key, None)
        if env:
            for key, value in env.items():
                if value is None:
                    run_env.pop(key, None)
                else:
                    run_env[key] = value
        return subprocess.run(
            [os.path.join(self.repo, "scripts", "release.sh"), *args],
            cwd=cwd or self.repo, env=run_env,
            capture_output=True, text=True, timeout=180,
        )

    def remote_log(self, remote, fmt="%an <%ae>|%cn <%ce>|%s"):
        proc = base.run(["git", "log", f"--format={fmt}", "main"], cwd=remote, check=False)
        if proc.returncode != 0:
            return []
        return proc.stdout.strip().splitlines()

    def staging_dirs(self):
        return sorted(
            name for name in os.listdir(self.script_tmp)
            if name.startswith("centrale-release.")
        )

    def temp_archives(self):
        return sorted(
            name for name in os.listdir(self.script_tmp)
            if name.startswith("centrale-release-archive.")
        )

    # -- the blocker: a foreign identity already on the remote -----------

    def test_a_remote_carrying_a_foreign_identity_is_refused(self):
        remote = self._bare_remote()
        head_before = self._seed_remote_with_foreign_commit(remote)

        proc = self.release("first release", remote=remote)

        self.assert_refused_untouched(proc, remote, head_before)
        self.assertIn(PRIVATE_EMAIL, proc.stderr)
        self.assertIn(REL_EMAIL, proc.stderr)
        # Named the actual cause, not just "something is wrong".
        self.assertIn("Add a README file", proc.stderr)

    # task-123: the guard used to compare author and committer EMAIL and
    # nothing else, so a commit wearing the release address under some
    # other display name walked straight through it and became an
    # unremovable ancestor of the public history. These three pin the
    # complete identity down: each varies exactly one field.

    def test_the_release_email_under_a_foreign_author_name_is_refused(self):
        """The reported hole. Same address as the release identity, a
        personal display name -- a full-identity check is the only thing
        that catches it."""
        remote = self._bare_remote()
        head_before = self._seed_remote_with_foreign_commit(
            remote, author=(PRIVATE_NAME, REL_EMAIL), committer=(REL_NAME, REL_EMAIL))

        proc = self.release("first release", remote=remote)

        self.assert_refused_untouched(proc, remote, head_before)
        # The diagnosis says WHICH half is wrong and what was expected,
        # which is the difference between a fixable message and a wall.
        self.assertIn(f"author: {PRIVATE_NAME} <{REL_EMAIL}>", proc.stderr)
        self.assertNotIn(f"committer: {REL_NAME} <{REL_EMAIL}>", proc.stderr)
        self.assertIn(f"{REL_NAME} <{REL_EMAIL}>", proc.stderr)

    def test_the_release_email_under_a_foreign_committer_name_is_refused(self):
        """The other half, and not a hypothetical one: a rebase or a
        GitHub web edit keeps the author and rewrites the committer."""
        remote = self._bare_remote()
        head_before = self._seed_remote_with_foreign_commit(
            remote, author=(REL_NAME, REL_EMAIL), committer=(PRIVATE_NAME, REL_EMAIL))

        proc = self.release("first release", remote=remote)

        self.assert_refused_untouched(proc, remote, head_before)
        self.assertIn(f"committer: {PRIVATE_NAME} <{REL_EMAIL}>", proc.stderr)
        self.assertNotIn(f"author: {REL_NAME} <{REL_EMAIL}>", proc.stderr)

    def test_the_release_name_over_a_foreign_email_is_still_refused(self):
        """The check the fix must not trade away: the original
        email-based refusal still fires when only the name matches."""
        remote = self._bare_remote()
        head_before = self._seed_remote_with_foreign_commit(
            remote, author=(REL_NAME, PRIVATE_EMAIL), committer=(REL_NAME, PRIVATE_EMAIL))

        proc = self.release("first release", remote=remote)

        self.assert_refused_untouched(proc, remote, head_before)
        self.assertIn(f"author: {REL_NAME} <{PRIVATE_EMAIL}>", proc.stderr)
        self.assertIn(f"committer: {REL_NAME} <{PRIVATE_EMAIL}>", proc.stderr)

    def test_the_release_identity_under_a_differently_cased_email_is_accepted(self):
        """Names are matched exactly (a display name is not an address);
        addresses are not case-sensitive, and git preserves whatever was
        typed. A remote seeded with the release identity at a shouted
        address is the same identity, and releases normally."""
        remote = self._bare_remote()
        shouted = REL_EMAIL.upper()
        self._seed_remote_with_foreign_commit(
            remote, author=(REL_NAME, shouted), committer=(REL_NAME, shouted))

        proc = self.release("release onto a shouted address", remote=remote)

        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(len(self.remote_log(remote)), 2)

    def test_a_release_identity_history_is_accepted(self):
        """The guard rejects foreign identities, not all history: a
        remote whose commits are all the release identity's releases
        normally."""
        remote = self._bare_remote()
        first = self.release("release one", remote=remote)
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)

        self._write(self.repo, "payload.txt", "v2\n")
        self._git("add", "-A", "-f")
        self._git("commit", "-q", "-m", "fixture v2")
        self._bump_version("0.2.0")

        second = self.release("release two", remote=remote)
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)

        log = self.remote_log(remote)
        self.assertEqual(len(log), 2, log)
        self.assertEqual(log[0], f"{REL_NAME} <{REL_EMAIL}>|{REL_NAME} <{REL_EMAIL}>|release two")
        self.assertEqual(log[1], f"{REL_NAME} <{REL_EMAIL}>|{REL_NAME} <{REL_EMAIL}>|release one")
        # The private identity never reaches the public side, and neither
        # does the private history: the release commits are not merely
        # unrelated to the private HEAD, they are not in the private repo's
        # object graph at all, which is what makes a stray `git push` from
        # there a non-fast-forward git refuses on its own.
        self.assertNotIn(PRIVATE_EMAIL, "\n".join(log))
        public_head = base.run(["git", "rev-parse", "main"], cwd=remote).stdout.strip()
        known_privately = base.run(
            ["git", "-C", self.repo, "cat-file", "-e", public_head + "^{commit}"], check=False)
        self.assertNotEqual(known_privately.returncode, 0,
                            "the public release commit must not exist in the private repo")
        root_parents = base.run(
            ["git", "log", "--max-parents=0", "--format=%H", "main"], cwd=remote).stdout.split()
        self.assertEqual(len(root_parents), 1,
                         "the public side must be one linear chain from a single root")

    # -- argument parsing ------------------------------------------------

    def test_a_dry_run_flag_after_the_message_is_still_a_flag(self):
        remote = self._bare_remote()

        proc = self.release("ship it", "--dry-run", remote=remote)

        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("dry run complete", proc.stdout)
        self.assertIn("Commit message:  ship it", proc.stdout)
        # The decisive assertion: nothing was pushed. This invocation used
        # to set MESSAGE="ship it --dry-run" with DRY_RUN=0 and publish.
        self.assertEqual(self.remote_log(remote), [])

    def test_an_unknown_flag_after_the_message_is_rejected(self):
        remote = self._bare_remote()

        proc = self.release("ship it", "--drynru", remote=remote)

        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("unknown option: --drynru", proc.stderr)
        self.assertEqual(self.remote_log(remote), [])

    def test_a_double_dash_ends_flag_parsing(self):
        """The escape hatch for a message that must start with a dash."""
        remote = self._bare_remote()

        proc = self.release("--dry-run", "--", "--dry-run is the message", remote=remote)

        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("Commit message:  --dry-run is the message", proc.stdout)
        self.assertEqual(self.remote_log(remote), [])

    def test_a_missing_message_is_a_usage_error(self):
        proc = self.release("--dry-run", remote=self._bare_remote())
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("missing release message", proc.stderr)

    # -- config precedence ------------------------------------------------

    def test_the_environment_beats_the_config_file(self):
        env_remote = self._bare_remote("env-remote.git")
        self._write(self.repo, ".release-remote", (
            'RELEASE_REMOTE="/nonexistent/from-the-file.git"\n'
            'RELEASE_AUTHOR_NAME="From The File"\n'
            'RELEASE_AUTHOR_EMAIL="file@example.invalid"\n'
        ))

        proc = self.release("--dry-run", "who wins", remote=env_remote)

        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn(f"Would push to:   {env_remote}", proc.stdout)
        self.assertIn(f"Commit identity: {REL_NAME} <{REL_EMAIL}>", proc.stdout)
        self.assertNotIn("from-the-file.git", proc.stdout)

    def test_the_config_file_is_used_when_the_environment_is_silent(self):
        file_remote = self._bare_remote("file-remote.git")
        self._write(self.repo, ".release-remote", (
            f'RELEASE_REMOTE="{file_remote}"\n'
            'RELEASE_AUTHOR_NAME="From The File"\n'
            'RELEASE_AUTHOR_EMAIL="file@example.invalid"\n'
        ))

        proc = self.release(
            "--dry-run", "file wins",
            remote=None,
            env={"RELEASE_AUTHOR_NAME": None, "RELEASE_AUTHOR_EMAIL": None},
        )

        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn(f"Would push to:   {file_remote}", proc.stdout)
        self.assertIn("Commit identity: From The File <file@example.invalid>", proc.stdout)

    def test_an_unconfigured_release_explains_itself(self):
        proc = self.release(
            "no config anywhere", remote=None,
            env={"RELEASE_AUTHOR_NAME": None, "RELEASE_AUTHOR_EMAIL": None},
        )
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("release not configured", proc.stderr)

    # -- the gate ---------------------------------------------------------

    def test_a_gate_failure_pushes_nothing_and_keeps_the_evidence(self):
        remote = self._bare_remote()
        self._write(self.repo, os.path.join("tests", "test_fixture.py"), FAILING_TEST_PY)
        self._git("add", "-A", "-f")
        self._git("commit", "-q", "-m", "break the suite")

        proc = self.release("should never ship", remote=remote)

        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("the test suite failed in the staged snapshot", proc.stderr)
        self.assertIn("failure of the SNAPSHOT", proc.stderr)
        self.assertEqual(self.remote_log(remote), [])
        # The staged snapshot IS the evidence -- it survives, its path is
        # printed, and it really is the tree that failed.
        kept = self.staging_dirs()
        self.assertEqual(len(kept), 1, kept)
        kept_path = os.path.join(self.script_tmp, kept[0])
        self.assertIn(kept_path, proc.stderr)
        self.assertTrue(os.path.exists(os.path.join(kept_path, "tests", "test_fixture.py")))
        # The archive tarball is temp scaffolding, not evidence: still cleaned up.
        self.assertEqual(self.temp_archives(), [])

    def test_a_seeded_secret_aborts_the_release_before_the_suite_runs(self):
        """task-109's gate, end to end: the scan is the FIRST gate step,
        so a snapshot carrying a credential shape never reaches the test
        suite, let alone the remote."""
        remote = self._bare_remote()
        # Assembled rather than written literally: this repo scans itself
        # in its own suite, so a literal key here would fail that check.
        self._write(self.repo, "settings.py",
                    'AWS_KEY = "' + "AKIA" + "X" * 16 + '"\n')
        self._git("add", "-A", "-f")
        self._git("commit", "-q", "-m", "seed a credential shape")

        proc = self.release("must not ship", remote=remote)

        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("looks like a secret or a personal identity", proc.stderr)
        self.assertIn("settings.py", proc.stdout)
        # Before the suite: a green suite is never the reason this passed.
        self.assertNotIn("the test suite failed", proc.stderr)
        self.assertNotIn("Ran 1 test", proc.stderr)
        self.assertEqual(self.remote_log(remote), [])

    def test_a_seeded_home_path_aborts_the_release(self):
        """The identity half of the same gate -- and the direction that
        matters most here, since a home path is what actually leaked."""
        remote = self._bare_remote()
        self._write(self.repo, "docs.md", "See /home/" + "someone" + "/notes.\n")
        self._git("add", "-A", "-f")
        self._git("commit", "-q", "-m", "seed a home path")

        proc = self.release("must not ship", remote=remote)

        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("looks like a secret or a personal identity", proc.stderr)
        self.assertIn("home-path", proc.stdout)
        self.assertEqual(self.remote_log(remote), [])

    def test_an_unarmed_scan_is_a_gate_failure_not_a_clean_release(self):
        """task-166: with no RELEASE_PRIVATE_NAMES anywhere, the scan
        cannot check the snapshot for private repo names at all -- and a
        check that ran none of its rules must not read as a pass. The
        refusal has to name the missing configuration rather than claim
        the snapshot is dirty, since the snapshot here is fine."""
        remote = self._bare_remote()

        proc = self.release("nothing armed the scan", remote=remote,
                            env={"RELEASE_PRIVATE_NAMES": None})

        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("could not be armed", proc.stderr)
        self.assertIn("UNARMED", proc.stdout)
        self.assertNotIn("looks like a secret", proc.stderr)
        self.assertEqual(self.remote_log(remote), [])

    def test_an_empty_private_name_list_is_a_declaration_and_releases(self):
        """The other half: "there are none" is an answer, not a gap, so
        an explicitly empty list arms the scan with nothing to match and
        the release goes through. Without this pairing the fix above
        would just be a release nobody can cut."""
        remote = self._bare_remote()

        proc = self.release("declared none", remote=remote,
                            env={"RELEASE_PRIVATE_NAMES": ""})

        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(len(self.remote_log(remote)), 1)

    def test_a_snapshot_with_no_scan_in_it_fails_rather_than_skips(self):
        """The failure mode worth a test of its own: if the scan were
        merely skipped when absent, deleting it would publish unscanned
        and nothing would say so."""
        remote = self._bare_remote()
        self._git("rm", "-q", os.path.join("scripts", "scan_release.py"))
        self._git("commit", "-q", "-m", "drop the scan")

        proc = self.release("unscanned", remote=remote)

        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("scan_release.py is missing from the staged snapshot",
                      proc.stderr)
        self.assertEqual(self.remote_log(remote), [])

    # -- the tested backlog baseline (task-156) --------------------------

    def test_a_machine_off_the_declared_baseline_cannot_cut_a_release(self):
        """The actual tie between a release and the CLI it claims to have
        been tested against. The snapshot here is perfectly good -- it is
        the MACHINE that is not the one its README describes -- so this
        has to read as a machine problem (exit 3), not a bad snapshot."""
        remote = self._bare_remote()
        # A baseline no machine will ever have installed, so this test
        # never depends on which `backlog` is actually present.
        self._write(self.repo, "version.py", self._version_py("0.1.0", baseline="0.0.1"))
        self._git("add", "-A", "-f")
        self._git("commit", "-q", "-m", "declare a baseline this machine is not on")

        proc = self.release("wrong CLI", remote=remote)

        self.assertEqual(proc.returncode, 3, proc.stdout + proc.stderr)
        self.assertIn("cannot run a release on THIS MACHINE", proc.stderr)
        self.assertIn("0.0.1", proc.stderr)
        self.assertIn(self.machine_backlog, proc.stderr)
        # Not dressed as a snapshot failure, and the way out is named.
        self.assertNotIn("failure of the SNAPSHOT", proc.stderr)
        self.assertIn("npm i -g backlog.md@0.0.1", proc.stderr)
        self.assertEqual(self.remote_log(remote), [])
        # Nothing wrong in the staged tree, so nothing is kept to inspect.
        self.assertEqual(self.staging_dirs(), [])
        self.assertEqual(self.temp_archives(), [])

    def test_the_same_snapshot_releases_from_a_machine_on_the_baseline(self):
        """The other half of the claim above: the refusal is about the
        machine, so the identical tree goes out untouched once the two
        numbers agree."""
        remote = self._bare_remote()
        self._write(self.repo, "version.py", self._version_py("0.1.0", baseline="0.0.1"))
        self._git("add", "-A", "-f")
        self._git("commit", "-q", "-m", "declare a baseline this machine is not on")
        self.assertEqual(self.release("wrong CLI", remote=remote).returncode, 3)

        self._write(self.repo, "version.py", self._version_py("0.1.0"))
        self._git("add", "-A", "-f")
        self._git("commit", "-q", "-m", "declare the baseline this machine is on")

        proc = self.release("right CLI", remote=remote)

        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("matches the snapshot's tested baseline", proc.stdout)
        self.assertEqual(len(self.remote_log(remote)), 1)

    def test_a_snapshot_declaring_no_baseline_at_all_fails_the_release(self):
        """The mirror image, and the reason this one is exit 1: a missing
        constant IS a fact about the snapshot. Without it nothing ties
        the release to a CLI, which is the whole point of the constant --
        so it fails rather than releasing untied."""
        remote = self._bare_remote()
        self._write(self.repo, "version.py", '__version__ = "0.1.0"\n')
        self._git("add", "-A", "-f")
        self._git("commit", "-q", "-m", "drop the baseline")

        proc = self.release("untied", remote=remote)

        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("could not read a usable TESTED_BACKLOG_VERSION", proc.stderr)
        self.assertEqual(self.remote_log(remote), [])

    def test_a_successful_release_cleans_up_after_itself(self):
        remote = self._bare_remote()
        proc = self.release("clean release", remote=remote)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(self.staging_dirs(), [])
        self.assertEqual(self.temp_archives(), [])

    def test_the_snapshot_carries_tracked_but_gitignored_paths(self):
        """`git add -A` honours the .gitignore that came with the archive;
        the script uses -f so the snapshot is exactly the archived tracked
        content, never a subset of it."""
        remote = self._bare_remote()
        proc = self.release("with an ignored-but-tracked path", remote=remote)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        shipped = base.run(["git", "ls-tree", "-r", "--name-only", "main"], cwd=remote).stdout.split()
        self.assertIn("ignored-but-tracked.txt", shipped)
        self.assertIn("server.py", shipped)

    # -- the version, the tag, and the changelog (task-107) ---------------

    def test_a_release_is_tagged_with_the_version_under_the_release_identity(self):
        remote = self._bare_remote()

        proc = self.release("first release", remote=remote)

        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(self.remote_tags(remote), ["v0.1.0"])
        # ANNOTATED, not lightweight: the public history gets real
        # release points rather than a chain of untitled squashes.
        self.assertEqual(
            base.run(["git", "cat-file", "-t", "v0.1.0"], cwd=remote).stdout.strip(),
            "tag")
        # And tagged under the RELEASE identity. An annotated tag records
        # a tagger of its own -- without the -c override that would be
        # the maintainer's real name and email, published permanently
        # beside a commit carefully authored not to be.
        tagger = base.run(
            ["git", "for-each-ref", "--format=%(taggername) <%(taggeremail)>",
             "refs/tags/v0.1.0"], cwd=remote).stdout.strip()
        self.assertIn(REL_NAME, tagger)
        self.assertIn(REL_EMAIL, tagger)
        self.assertNotIn(PRIVATE_EMAIL, tagger)
        # It names the release commit itself, not some earlier point.
        self.assertEqual(
            base.run(["git", "rev-list", "-n", "1", "v0.1.0"], cwd=remote).stdout.strip(),
            base.run(["git", "rev-parse", "main"], cwd=remote).stdout.strip())

    def test_releasing_changed_content_under_a_published_version_is_refused(self):
        """The forcing function: content moved, the number did not."""
        remote = self._bare_remote()
        first = self.release("release one", remote=remote)
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)

        self._write(self.repo, "payload.txt", "v2\n")
        self._git("add", "-A", "-f")
        self._git("commit", "-q", "-m", "fixture v2 without a bump")

        proc = self.release("release one again", remote=remote)

        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("v0.1.0 is already published", proc.stderr)
        # Says what to do, not just what went wrong.
        self.assertIn("version.py", proc.stderr)
        self.assertIn("CHANGELOG.md", proc.stderr)
        # Nothing published: one release commit, one tag, both unmoved.
        self.assertEqual(len(self.remote_log(remote)), 1)
        self.assertEqual(self.remote_tags(remote), ["v0.1.0"])

    def test_unchanged_content_is_still_nothing_to_release(self):
        """The benign case must not be reported as a tag collision: the
        "nothing changed" exit runs before the tag check."""
        remote = self._bare_remote()
        first = self.release("release one", remote=remote)
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)

        proc = self.release("release one again", remote=remote)

        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("nothing changed since the last release", proc.stdout)
        self.assertNotIn("already published", proc.stderr)
        self.assertEqual(len(self.remote_log(remote)), 1)

    def test_a_bumped_version_releases_and_tags_again(self):
        remote = self._bare_remote()
        self.assertEqual(self.release("release one", remote=remote).returncode, 0)

        self._write(self.repo, "payload.txt", "v2\n")
        self._git("add", "-A", "-f")
        self._git("commit", "-q", "-m", "fixture v2")
        self._bump_version("0.2.0")

        proc = self.release("release two", remote=remote)

        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(sorted(self.remote_tags(remote)), ["v0.1.0", "v0.2.0"])
        self.assertEqual(
            base.run(["git", "rev-list", "-n", "1", "v0.2.0"], cwd=remote).stdout.strip(),
            base.run(["git", "rev-parse", "main"], cwd=remote).stdout.strip())

    def test_a_bump_with_no_changelog_entry_is_a_gate_failure(self):
        remote = self._bare_remote()
        self.assertEqual(self.release("release one", remote=remote).returncode, 0)

        self._bump_version("0.2.0", changelog=False)

        proc = self.release("release two", remote=remote)

        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("CHANGELOG.md has no '## v0.2.0' section", proc.stderr)
        self.assertEqual(len(self.remote_log(remote)), 1)
        self.assertEqual(self.remote_tags(remote), ["v0.1.0"])

    def test_a_snapshot_with_no_version_constant_is_refused(self):
        remote = self._bare_remote()
        self._git("rm", "-q", "version.py")
        self._git("commit", "-q", "-m", "drop the version")

        proc = self.release("unversioned", remote=remote)

        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("version.py is missing from the staged snapshot", proc.stderr)
        self.assertEqual(self.remote_log(remote), [])
        self.assertEqual(self.remote_tags(remote), [])

    def test_a_dry_run_names_the_tag_it_would_cut_and_pushes_nothing(self):
        remote = self._bare_remote()

        proc = self.release("--dry-run", "preview", remote=remote)

        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("Release tag:     v0.1.0 (annotated, same identity)", proc.stdout)
        self.assertEqual(self.remote_log(remote), [])
        self.assertEqual(self.remote_tags(remote), [])

    # -- this machine vs. the snapshot ------------------------------------

    def test_a_missing_tool_is_reported_as_a_machine_problem(self):
        """A release machine without `backlog` fails the gate's
        `server.py --check` for reasons that say nothing about the
        snapshot. That must not read like a bad snapshot -- and it gets
        its own exit code.

        task-130: this used to hide `backlog` by trimming PATH to
        /usr/bin:/bin and SKIP on a machine where backlog lives there --
        the one skip in this module that no tool check explained, and
        under the release gate a skip is a failure. The sandbox PATH
        hides exactly one tool and nothing else, on any machine."""
        sandbox = self._path_without("backlog")
        self.assertIsNone(shutil.which("backlog", path=sandbox))
        for tool in ("git", "tar", "python3", "node", "tmux"):
            self.assertIsNotNone(shutil.which(tool, path=sandbox), tool)
        remote = self._bare_remote()

        proc = self.release("no tooling here", remote=remote, env={"PATH": sandbox})

        self.assertEqual(proc.returncode, 3, proc.stdout + proc.stderr)
        self.assertIn("THIS MACHINE", proc.stderr)
        self.assertIn("backlog", proc.stderr)
        self.assertIn("never built or evaluated", proc.stderr)
        self.assertEqual(self.remote_log(remote), [])
        self.assertEqual(self.staging_dirs(), [])

    # -- the frontend behavioural tier may not skip its way past a release --

    def test_a_release_machine_without_node_is_refused_before_anything_is_built(self):
        """task-124: `node` is a release-machine prerequisite.

        Without it Centrale's frontend behavioural tier does not fail --
        it SKIPS, and `discover tests` still reports OK, so the gate
        would pass on a snapshot whose JavaScript nothing had parsed,
        let alone run. That has to be caught where it is true (this
        machine), before a snapshot is built or a remote is contacted."""
        sandbox = self._path_without("node")
        # The sandbox is the real PATH minus one entry: every other
        # prerequisite must still resolve, or this test would prove
        # nothing about `node` specifically.
        self.assertIsNone(shutil.which("node", path=sandbox))
        for tool in ("git", "tar", "python3", "backlog"):
            self.assertIsNotNone(shutil.which(tool, path=sandbox), tool)
        remote = self._bare_remote()

        proc = self.release("no node here", remote=remote, env={"PATH": sandbox})

        self.assertEqual(proc.returncode, 3, proc.stdout + proc.stderr)
        self.assertIn("THIS MACHINE", proc.stderr)
        self.assertIn("node", proc.stderr)
        # Named as the machine's problem, with the reason spelled out --
        # a bare "missing: node" would read like a broken snapshot.
        self.assertIn("never built or evaluated", proc.stderr)
        self.assertIn("SKIPS", proc.stderr)
        # Nothing built, nothing cloned, nothing pushed.
        self.assertEqual(self.staging_dirs(), [])
        self.assertEqual(self.temp_archives(), [])
        self.assertEqual(self.remote_log(remote), [])
        self.assertEqual(self.remote_tags(remote), [])

    def test_a_tier_that_skips_for_want_of_its_runtime_fails_the_gate(self):
        """The other half: node on PATH is not the same as the tier
        having run. The gate runs the snapshot's suite with
        CENTRALE_REQUIRE_NODE=1, so a tier that finds no runtime anyway
        fails the release instead of quietly skipping inside an OK."""
        self._write(self.repo, os.path.join("tests", "test_tier.py"),
                    RUNTIME_TIER_TEST_PY)
        self._git("add", "-A", "-f")
        self._git("commit", "-q", "-m", "add a runtime-dependent tier")
        remote = self._bare_remote()

        proc = self.release(
            "skipping tier", remote=remote,
            env={"FIXTURE_TIER_RUNTIME": "definitely-not-a-real-runtime"})

        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("the test suite failed in the staged snapshot", proc.stderr)
        self.assertIn("CENTRALE_REQUIRE_NODE=1", proc.stderr)
        # The tier's own words reached the operator, not just "1 failed".
        self.assertIn("definitely-not-a-real-runtime", proc.stdout + proc.stderr)
        self.assertEqual(self.remote_log(remote), [])
        self.assertEqual(self.remote_tags(remote), [])

    def test_the_same_tier_still_skips_cleanly_outside_a_release(self):
        """AC #4, kept honest from the other side: the strictness is the
        release's, not the suite's. The identical module, run the way a
        developer runs it, skips and reports OK."""
        self._write(self.repo, os.path.join("tests", "test_tier.py"),
                    RUNTIME_TIER_TEST_PY)
        env = dict(os.environ)
        env["FIXTURE_TIER_RUNTIME"] = "definitely-not-a-real-runtime"
        env.pop("CENTRALE_REQUIRE_NODE", None)

        proc = subprocess.run(
            ["python3", "-m", "unittest", "discover", "tests"],
            cwd=self.repo, env=env, capture_output=True, text=True, timeout=60)

        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("OK (skipped=1)", proc.stderr)

    def test_the_gate_runs_the_snapshot_suite_under_the_strict_switch(self):
        """And a tier that CAN run is unaffected by the switch: the same
        module, with its runtime present, passes the gate and the
        release goes out."""
        self._write(self.repo, os.path.join("tests", "test_tier.py"),
                    RUNTIME_TIER_TEST_PY)
        self._git("add", "-A", "-f")
        self._git("commit", "-q", "-m", "add a runtime-dependent tier")
        remote = self._bare_remote()

        # `git` stands in for the runtime here: present on any machine
        # this tier runs on at all (the class requires it).
        proc = self.release("tier runs", remote=remote,
                            env={"FIXTURE_TIER_RUNTIME": "git"})

        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(len(self.remote_log(remote)), 1)

    # -- the integration tier is part of the gate (task-130) --------------

    def test_the_gate_runs_the_integration_tier_against_the_staged_snapshot(self):
        """The tier RUNS, runs against the STAGED tree (not the checkout
        the script was invoked from), and runs under the strict switch:
        all three read back from the marker the fixture's tier writes."""
        marker = os.path.join(self.tmp_dir, "itest-ran.txt")
        remote = self._bare_remote()

        proc = self.release("with the tier", remote=remote,
                            env={"FIXTURE_ITEST_MARKER": marker})

        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertTrue(os.path.exists(marker), "the integration tier never ran")
        with open(marker, encoding="utf-8") as f:
            ran_from, switch = f.read().splitlines()[:2]
        self.assertTrue(
            ran_from.startswith(os.path.join(self.script_tmp, "centrale-release.")),
            f"the tier ran from {ran_from}, not from a staged snapshot")
        self.assertFalse(ran_from.startswith(self.repo),
                         "the tier ran against the checkout, not the snapshot")
        self.assertEqual(switch, "1", "CENTRALE_REQUIRE_INTEGRATION=1 did not reach the tier")
        self.assertEqual(len(self.remote_log(remote)), 1)

    def test_a_failing_integration_test_aborts_the_release_after_the_unit_tier(self):
        remote = self._bare_remote()
        self._write(self.repo, os.path.join("tests_integration", "test_fixture_integration.py"),
                    FAILING_ITEST_PY)
        self._git("add", "-A", "-f")
        self._git("commit", "-q", "-m", "break the integration tier")

        proc = self.release("should never ship", remote=remote)

        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("the integration tier failed in the staged snapshot", proc.stderr)
        self.assertIn("CENTRALE_REQUIRE_INTEGRATION=1", proc.stderr)
        self.assertIn("failure of the SNAPSHOT", proc.stderr)
        # The tier's own words reached the operator.
        self.assertIn("deliberate integration-tier failure", proc.stdout + proc.stderr)
        # AFTER the unit tier: that one passed (its OK precedes the
        # failure), and the failure is not misreported as its.
        self.assertNotIn("the test suite failed", proc.stderr)
        self.assertLess(proc.stderr.index("\nOK\n"), proc.stderr.index("FAILED (failures=1)"))
        # Nothing pushed, and the snapshot that failed is kept.
        self.assertEqual(self.remote_log(remote), [])
        self.assertEqual(self.remote_tags(remote), [])
        kept = self.staging_dirs()
        self.assertEqual(len(kept), 1, kept)
        self.assertIn(os.path.join(self.script_tmp, kept[0]), proc.stderr)

    def test_a_snapshot_with_no_integration_tier_fails_rather_than_skips(self):
        """The task-109 rule, applied to the tier: if a missing
        tests_integration/ were merely skipped, deleting it would publish
        code whose real subprocess behaviour nothing had exercised, and
        nothing would say so."""
        remote = self._bare_remote()
        self._git("rm", "-q", "-r", "tests_integration")
        self._git("commit", "-q", "-m", "drop the integration tier")

        proc = self.release("untested", remote=remote)

        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("tests_integration/ is missing from the staged snapshot", proc.stderr)
        # The unit tier had already passed: the tier is checked after it.
        self.assertIn("\nOK\n", proc.stderr)
        self.assertEqual(self.remote_log(remote), [])

    def test_a_release_machine_without_tmux_is_refused_before_anything_is_built(self):
        """`tmux` is a release-machine prerequisite for the reason `node`
        is: without it the tier's tool-gated modules SKIP rather than
        fail, and that has to be caught where it is true (this machine),
        before a snapshot is built or a remote is contacted."""
        sandbox = self._path_without("tmux")
        self.assertIsNone(shutil.which("tmux", path=sandbox))
        for tool in ("git", "tar", "python3", "backlog", "node"):
            self.assertIsNotNone(shutil.which(tool, path=sandbox), tool)
        remote = self._bare_remote()

        proc = self.release("no tmux here", remote=remote, env={"PATH": sandbox})

        self.assertEqual(proc.returncode, 3, proc.stdout + proc.stderr)
        self.assertIn("THIS MACHINE", proc.stderr)
        self.assertIn("tmux", proc.stderr)
        self.assertIn("never built or evaluated", proc.stderr)
        self.assertIn("SKIP", proc.stderr)
        self.assertEqual(self.staging_dirs(), [])
        self.assertEqual(self.temp_archives(), [])
        self.assertEqual(self.remote_log(remote), [])
        self.assertEqual(self.remote_tags(remote), [])

    def test_an_integration_test_that_skips_for_want_of_a_tool_fails_the_gate(self):
        """tmux on PATH is not the same as the tier having run. The gate
        exports CENTRALE_REQUIRE_INTEGRATION=1, so a test that finds its
        tool missing anyway fails the release instead of quietly
        skipping inside an OK."""
        self._write(self.repo, os.path.join("tests_integration", "test_tool_tier.py"),
                    TOOL_TIER_ITEST_PY)
        self._git("add", "-A", "-f")
        self._git("commit", "-q", "-m", "add a tool-dependent integration test")
        remote = self._bare_remote()

        proc = self.release(
            "skipping tier", remote=remote,
            env={"FIXTURE_ITEST_TOOL": "definitely-not-a-real-tool"})

        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("the integration tier failed in the staged snapshot", proc.stderr)
        self.assertIn("definitely-not-a-real-tool", proc.stdout + proc.stderr)
        self.assertEqual(self.remote_log(remote), [])
        self.assertEqual(self.remote_tags(remote), [])

    def test_the_same_integration_test_still_skips_cleanly_outside_a_release(self):
        """The strictness is the release's, not the tier's: the identical
        module, run the way a developer runs it, skips and reports OK."""
        self._write(self.repo, os.path.join("tests_integration", "test_tool_tier.py"),
                    TOOL_TIER_ITEST_PY)
        env = dict(os.environ)
        env["FIXTURE_ITEST_TOOL"] = "definitely-not-a-real-tool"
        env.pop("CENTRALE_REQUIRE_INTEGRATION", None)

        proc = subprocess.run(
            ["python3", "-m", "unittest", "discover", "tests_integration"],
            cwd=self.repo, env=env, capture_output=True, text=True, timeout=60)

        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("OK (skipped=1)", proc.stderr)


if __name__ == "__main__":
    unittest.main()
