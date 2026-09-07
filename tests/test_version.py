"""The version: one constant, how it is resolved, and what is derived from it.

task-107. Nothing in the codebase used to answer "which build am I
running". These tests hold the three ends of the answer together: the
constant in `version.py`, the string the running server reports (the
constant, with `git describe` layered on when there is a repository to
describe), and the tag `scripts/release.sh` derives from the constant.

Every git call goes through `server.run_git`, so the resolution is
exercised without a repository -- including the two cases a real
checkout cannot produce on demand: a downloaded zip with no `.git` at
all, and a machine with no `git` binary.
"""

import ast
import os
import re
import subprocess
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server  # noqa: E402
import version  # noqa: E402

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _proc(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


def _read(*parts):
    with open(os.path.join(REPO_ROOT, *parts), encoding="utf-8") as f:
        return f.read()


class VersionConstantTests(unittest.TestCase):
    def test_the_constant_is_a_plain_dotted_release_number(self):
        # release.sh derives the tag from this with a `[0-9]*.[0-9]*.[0-9]*`
        # case pattern and refuses anything else, so a constant it could
        # not tag has to fail here rather than at release time.
        self.assertRegex(version.__version__, r"^\d+\.\d+\.\d+$")

    def test_the_module_is_only_its_two_constants(self):
        # Its whole reason for existing separately: release.sh imports it
        # inside a bare staged snapshot to read the tag. Anything that
        # runs at import -- a filesystem read, a subprocess, another
        # import of this project's own modules -- could fail there, in
        # the middle of a release, for a reason that has nothing to do
        # with the version.
        body = ast.parse(_read("version.py")).body
        self.assertIsInstance(body[0], ast.Expr, "expected the module docstring first")
        statements = body[1:]
        self.assertEqual(
            [type(node).__name__ for node in statements], ["Assign", "Assign"],
            "version.py must be its docstring and its two bare constants, nothing "
            "else: release.sh imports it out of a staged snapshot to derive the tag "
            "and read the tested backlog baseline, where anything that runs at "
            "import can fail mid-release for a reason that has nothing to do with "
            "either number")
        self.assertEqual(
            [t.id for node in statements for t in node.targets],
            ["__version__", "TESTED_BACKLOG_VERSION"])
        self.assertEqual(statements[0].value.value, version.__version__)
        self.assertEqual(statements[1].value.value, version.TESTED_BACKLOG_VERSION)

    def test_the_changelog_has_a_section_for_the_current_version(self):
        # The same invariant release.sh's gate enforces at release time,
        # checked here so "bumped the constant, forgot the entry" fails
        # in the suite instead of against the remote.
        changelog = _read("CHANGELOG.md")
        self.assertRegex(
            changelog,
            r"(?m)^#+\s+v%s([^0-9.]|$)" % re.escape(version.__version__),
            f"CHANGELOG.md has no '## v{version.__version__}' section")


class TestedBacklogBaselineTests(unittest.TestCase):
    """task-156: the `backlog` CLI version this release claims to have
    been verified against.

    Centrale is a veneer over that CLI, so the claim is load bearing --
    and it lived only in README prose, on a dependency that shipped 100
    releases in its last 355 days. These tests hold the same three ends
    together the version constant's do: the constant, the documents that
    state it to a reader, and the release script that refuses to publish
    from a machine running something else.

    The CHANGELOG is deliberately NOT among them, and it is worth saying
    so before someone adds it: those entries describe releases in the
    past tense, so an older one naming an older baseline is correct
    history rather than drift. Only the constant, the two live
    documents, and the machine a release is cut on have to agree.
    """

    def test_the_baseline_is_a_plain_dotted_version(self):
        # release.sh matches it with a `[0-9]*.[0-9]*.[0-9]*` case
        # pattern and refuses anything else, and compares it verbatim
        # against what `backlog --version` printed.
        self.assertRegex(version.TESTED_BACKLOG_VERSION, r"^\d+\.\d+\.\d+$")

    def test_the_readme_states_the_version_the_constant_declares(self):
        # The paragraph a user reads before installing anything. It is
        # the reason the constant exists: prose nothing checks is how
        # "verified against v1.50.1" survives three CLI releases.
        readme = _read("README.md")
        stated = re.findall(r"verified against\s+`backlog`\s+v(\d+\.\d+\.\d+)", readme)
        self.assertEqual(
            len(stated), 1,
            "README.md should state the tested `backlog` version exactly once, as "
            "\"verified against `backlog` vX.Y.Z\"; found %d such claims" % len(stated))
        self.assertEqual(
            stated[0], version.TESTED_BACKLOG_VERSION,
            "README.md says Centrale is verified against backlog v%s, but "
            "version.py's TESTED_BACKLOG_VERSION is %s. The constant is the source "
            "of truth (--check and the release gate both read it); the README is "
            "what a reader believes. Bumping the baseline updates both -- see "
            "docs/operations.md, \"Versioning\"." % (stated[0], version.TESTED_BACKLOG_VERSION))

    def test_the_operations_chapter_states_the_same_version(self):
        # One document over, the same claim wearing different words: the
        # JSON contract's "verified against CLI vX.Y.Z". Two independent
        # copies of one number is exactly the drift this pins.
        doc = _read("docs", "operations.md")
        stated = re.findall(r"verified\s*\n?\s*against CLI v(\d+\.\d+\.\d+)", doc)
        self.assertEqual(
            len(stated), 1,
            "docs/operations.md should state the verified `backlog` CLI version "
            "exactly once, as \"verified against CLI vX.Y.Z\"; found %d" % len(stated))
        self.assertEqual(stated[0], version.TESTED_BACKLOG_VERSION)


class ReleaseBaselineGateTests(unittest.TestCase):
    """task-156: release.sh's refusal, asserted against the script's text
    for the same reason ReleaseTagDerivationTests is -- running it for
    real would clone and push."""

    @classmethod
    def script(cls):
        return _read("scripts", "release.sh")

    def test_the_baseline_is_read_from_the_staged_snapshot(self):
        # The same rule as the tag: the claim that has to be true is the
        # one being published, never whatever the working tree was
        # edited to say a moment ago.
        self.assertIn(
            'TESTED_BACKLOG="$(cd "$STAGING_DIR" && python3 -c '
            "'import version; print(version.TESTED_BACKLOG_VERSION)'",
            self.script())

    def test_a_mismatch_aborts_as_a_machine_problem_not_a_snapshot_failure(self):
        # The distinction the exit codes exist for: this snapshot is
        # fine and will release unchanged from a machine on the
        # baseline, so it must not be reported (or exited) as a bad
        # snapshot.
        script = self.script()
        start = script.index('if [ "$MACHINE_BACKLOG" != "$TESTED_BACKLOG" ]; then')
        end = script.index("\nfi\n", start)
        block = script[start:end]
        self.assertIn("cannot run a release on THIS MACHINE", block)
        self.assertIn('exit "$EXIT_ENV"', block)
        self.assertNotIn("gate_failed", block)

    def test_the_comparison_happens_before_anything_is_committed_or_tagged(self):
        # An abort here has changed nothing anywhere -- and the staging
        # tree is not kept for inspection, because nothing in it is
        # wrong.
        script = self.script()
        self.assertLess(script.index('MACHINE_BACKLOG="$(backlog --version'),
                        script.index('tag -a "$RELEASE_TAG"'))

    def test_a_snapshot_without_the_constant_is_a_release_failure(self):
        # The mirror image: an unreadable baseline IS a fact about the
        # snapshot, and exits 1 the way an unreadable version does.
        script = self.script()
        start = script.index('TESTED_BACKLOG="$(cd "$STAGING_DIR"')
        block = script[start:script.index('MACHINE_BACKLOG=', start)]
        self.assertIn("could not read a usable TESTED_BACKLOG_VERSION", block)
        self.assertIn("exit 1", block)


class DetectVersionTests(unittest.TestCase):
    """What the server resolves ONCE, at startup, as the build it runs."""

    def setUp(self):
        self.constant = f"v{version.__version__}"

    def _detect(self, *, has_git_binary=True, has_dot_git=True, proc=None):
        with mock.patch.object(server, "which",
                               side_effect=lambda name: "/usr/bin/git" if (name == "git" and has_git_binary) else None), \
             mock.patch.object(server.os.path, "exists", return_value=has_dot_git), \
             mock.patch.object(server, "run_git", return_value=proc or _proc()) as run_git:
            return server.detect_version(), run_git

    def test_a_downloaded_zip_with_no_git_directory_reports_the_constant(self):
        # The case the design refused to paper over with a generated
        # file stamped into the snapshot: the constant already answers
        # it, correctly, and ships as ordinary tracked content.
        resolved, run_git = self._detect(has_dot_git=False)
        self.assertEqual(resolved, self.constant)
        run_git.assert_not_called()

    def test_a_machine_without_git_reports_the_constant(self):
        resolved, run_git = self._detect(has_git_binary=False)
        self.assertEqual(resolved, self.constant)
        run_git.assert_not_called()

    def test_a_released_snapshot_reports_the_tag_alone(self):
        # release.sh tagged the release commit, so describe returns the
        # version itself -- richer than the constant only in that it is
        # proof the tag is there.
        resolved, _ = self._detect(proc=_proc(0, self.constant + "\n"))
        self.assertEqual(resolved, self.constant)

    def test_a_development_checkout_reports_the_describe_suffix(self):
        # The suffix moves with EVERY commit, which is the property that
        # makes this answer "is the running server behind the code".
        described = self.constant + "-14-g4570911-dirty"
        resolved, _ = self._detect(proc=_proc(0, described + "\n"))
        self.assertEqual(resolved, described)

    def test_an_untagged_checkout_reports_both_the_version_and_the_commit(self):
        # Before the first release there is no tag to describe, so
        # `--always` falls back to a bare hash: it names the build but
        # not the version the changelog and the tag are keyed to, so
        # neither half is dropped.
        resolved, _ = self._detect(proc=_proc(0, "e773bdb-dirty\n"))
        self.assertEqual(resolved, f"{self.constant} (e773bdb-dirty)")

    def test_a_failed_or_empty_git_falls_back_to_the_constant(self):
        for proc in (_proc(128, "", "fatal: not a git repository"),
                     _proc(124, "", "timed out"),
                     _proc(0, "   \n")):
            with self.subTest(returncode=proc.returncode, stdout=proc.stdout):
                resolved, _ = self._detect(proc=proc)
                self.assertEqual(resolved, self.constant)

    def test_it_describes_the_repository_it_is_running_from(self):
        _, run_git = self._detect(proc=_proc(0, "v1.2.3\n"))
        run_git.assert_called_once_with(
            ["describe", "--tags", "--always", "--dirty"], cwd=server.BASE_DIR)


class ServedVersionTests(unittest.TestCase):
    """What a request is told -- read back, never re-derived."""

    def test_it_returns_what_startup_captured(self):
        with mock.patch.object(server, "detect_version", side_effect=AssertionError(
                "served_version must not re-resolve the version")):
            self.assertEqual(
                server.served_version({"version": "v0.1.0-3-gabc1234"}),
                "v0.1.0-3-gabc1234")

    def test_it_fails_open_to_the_constant_for_a_config_that_never_booted(self):
        # A config built directly by a test never went through main(),
        # the same missing-key situation tmux_capability() fails open on.
        for config in ({}, {"version": None}, {"version": ""}):
            with self.subTest(config=config):
                self.assertEqual(server.served_version(config),
                                 f"v{version.__version__}")

    def test_main_captures_it_once_into_the_config(self):
        # The whole point of capturing at startup rather than reading per
        # request: what the footer says is what THIS process booted from.
        # Asserted against main()'s source because running main() means
        # binding a port; the behaviour it produces is covered by the
        # /api/board tests in test_server.py.
        source = _read("server.py")
        main_body = source[source.index("\ndef main():"):]
        self.assertIn('config["version"] = detect_version()', main_body)


class LoadedCommitTests(unittest.TestCase):
    """Task-128: reading the commit back out of the string the server
    captured at boot -- the loaded side of the staleness comparison, and
    the only thing it is allowed to remember."""

    def test_the_describe_suffix_names_the_commit_dirty_or_not(self):
        self.assertEqual(server.loaded_commit("v0.1.0-14-g4570911"), ("4570911", False))
        self.assertEqual(server.loaded_commit("v0.1.0-14-g4570911-dirty"), ("4570911", False))

    def test_the_untagged_shape_names_the_bare_hash(self):
        self.assertEqual(server.loaded_commit("v0.1.0 (e773bdb)"), ("e773bdb", False))
        self.assertEqual(server.loaded_commit("v0.1.0 (e773bdb-dirty)"), ("e773bdb", False))

    def test_an_exact_tag_is_returned_as_a_tag_still_to_be_resolved(self):
        self.assertEqual(server.loaded_commit("v0.1.0"), ("v0.1.0", True))
        self.assertEqual(server.loaded_commit("v0.1.0-dirty"), ("v0.1.0", True))

    def test_anything_unrecognisable_is_none(self):
        for served in (None, "", "   ", 42, "not a version at all"):
            with self.subTest(served=served):
                self.assertIsNone(server.loaded_commit(served))


class CodeDriftTests(unittest.TestCase):
    """Task-128: whether the running process is behind the checkout it
    runs from -- derived from git on every call, quiet on any doubt."""

    HEAD = "a088d15" + "0" * 33

    def _drift(self, served, *, head=None, count="23\n", count_rc=0, tag_hash=None,
               has_git_binary=True, has_dot_git=True):
        """Drives code_drift() with a git that answers `rev-parse HEAD`
        with `head`, `rev-list --count` with `count`, and a tag lookup
        with `tag_hash`; records every git call made."""
        head = self.HEAD if head is None else head
        calls = []

        def fake_git(args, cwd=None):
            calls.append((args, cwd))
            if args == ["rev-parse", "HEAD"]:
                return _proc(0, head + "\n") if head else _proc(128, "", "fatal: not a git repository")
            if args[:2] == ["rev-list", "--count"]:
                return _proc(count_rc, count)
            if args[:3] == ["rev-parse", "--verify", "--quiet"]:
                return _proc(0, tag_hash + "\n") if tag_hash else _proc(1, "")
            raise AssertionError(f"unexpected git call: {args}")

        config = {} if served is None else {"version": served}
        with mock.patch.object(server, "which",
                               side_effect=lambda name: "/usr/bin/git" if (name == "git" and has_git_binary) else None), \
             mock.patch.object(server.os.path, "exists", return_value=has_dot_git), \
             mock.patch.object(server, "run_git", side_effect=fake_git):
            return server.code_drift(config), calls

    def test_a_checkout_that_moved_past_the_loaded_commit_is_reported(self):
        # The 2026-09-04 case: a process that booted from 055ff59 while
        # the checkout went on to a088d15, 23 commits later.
        drift, calls = self._drift("v0.1.0 (055ff59)")
        self.assertEqual(drift, {"loaded": "055ff59", "current": "a088d15", "commitsBehind": 23})
        self.assertEqual([a for a, _ in calls],
                         [["rev-parse", "HEAD"], ["rev-list", "--count", "055ff59..HEAD"]])
        self.assertTrue(all(cwd == server.BASE_DIR for _, cwd in calls))

    def test_the_describe_suffix_shape_is_compared_the_same_way(self):
        drift, _ = self._drift("v0.1.0-14-g4570911")
        self.assertEqual(drift, {"loaded": "4570911", "current": "a088d15", "commitsBehind": 23})

    def test_a_head_still_at_the_loaded_commit_is_silent(self):
        drift, calls = self._drift("v0.1.0-14-ga088d15", head=self.HEAD)
        self.assertIsNone(drift)
        # Nothing to count when nothing moved.
        self.assertEqual([a for a, _ in calls], [["rev-parse", "HEAD"]])

    def test_a_dirty_tree_on_the_loaded_side_is_not_staleness(self):
        # The process loaded this commit plus uncommitted edits; HEAD is
        # still this commit. A restart would change nothing git can see.
        drift, _ = self._drift("v0.1.0-14-ga088d15-dirty", head=self.HEAD)
        self.assertIsNone(drift)
        drift, _ = self._drift("v0.1.0 (a088d15-dirty)", head=self.HEAD)
        self.assertIsNone(drift)

    def test_the_current_commit_is_shown_to_the_loaded_commits_width(self):
        # An abbreviation git chose to be 9 wide is compared and shown
        # at 9, so the two hashes in the banner line up.
        drift, _ = self._drift("v0.1.0-14-g4570911ab")
        self.assertEqual(drift["current"], "a088d1500")

    def test_an_exact_tag_is_resolved_before_comparing(self):
        drift, calls = self._drift("v0.1.0", tag_hash="055ff59" + "f" * 33)
        self.assertEqual(drift, {"loaded": "v0.1.0", "current": "a088d15", "commitsBehind": 23})
        self.assertEqual(calls[0][0], ["rev-parse", "--verify", "--quiet", "v0.1.0^{commit}"])
        self.assertEqual(calls[-1][0], ["rev-list", "--count", "055ff59" + "f" * 33 + "..HEAD"])
        # And silent when HEAD is the tagged commit -- tag or not, the
        # process is running what is on disk.
        drift, _ = self._drift("v0.1.0-dirty", tag_hash=self.HEAD)
        self.assertIsNone(drift)

    def test_a_tag_git_cannot_resolve_is_silent(self):
        # The constant alone is also what a no-git boot reports, so an
        # unresolvable tag is "cannot tell", never "behind".
        drift, _ = self._drift("v0.1.0", tag_hash=None)
        self.assertIsNone(drift)

    def test_a_config_that_never_booted_has_nothing_to_compare(self):
        drift, calls = self._drift(None)
        self.assertIsNone(drift)
        self.assertEqual(calls, [])

    def test_a_published_snapshot_without_a_git_directory_is_silent(self):
        drift, calls = self._drift("v0.1.0-14-g4570911", has_dot_git=False)
        self.assertIsNone(drift)
        self.assertEqual(calls, [])

    def test_a_machine_without_git_is_silent(self):
        drift, calls = self._drift("v0.1.0-14-g4570911", has_git_binary=False)
        self.assertIsNone(drift)
        self.assertEqual(calls, [])

    def test_a_git_that_fails_or_answers_nonsense_is_silent(self):
        for head in ("", "not-a-hash", "abc123"):
            with self.subTest(head=head):
                drift, _ = self._drift("v0.1.0-14-g4570911", head=head)
                self.assertIsNone(drift)

    def test_an_unrecognisable_loaded_version_is_silent(self):
        drift, calls = self._drift("something else entirely")
        self.assertIsNone(drift)
        self.assertEqual(calls, [])

    def test_a_count_git_cannot_make_still_reports_the_drift(self):
        # The loaded commit rewritten away (a rebase, an amend): the two
        # still differ, and that is the finding; only the count is lost.
        drift, _ = self._drift("v0.1.0-14-g4570911", count="", count_rc=128)
        self.assertEqual(drift, {"loaded": "4570911", "current": "a088d15", "commitsBehind": None})

    def test_it_never_calls_detect_version(self):
        # The loaded side is what main() captured, full stop: re-describing
        # the checkout here would compare HEAD with itself.
        with mock.patch.object(server, "detect_version", side_effect=AssertionError(
                "code_drift must not re-resolve the version")):
            drift, _ = self._drift("v0.1.0-14-g4570911")
        self.assertEqual(drift["loaded"], "4570911")


class DoctorReportsTheVersionTests(unittest.TestCase):
    def test_check_names_the_build_before_anything_else(self):
        with mock.patch.object(server, "which", return_value=None), \
             mock.patch.object(server, "detect_version", return_value="v1.2.3-4-gfeedface"), \
             mock.patch.object(server, "load_config", return_value={"projects": []}):
            lines, _ok = server.run_doctor_check(config_path="/nonexistent/projects.json")
        self.assertEqual(lines[0], "[PASS] Centrale v1.2.3-4-gfeedface")


class ReleaseTagDerivationTests(unittest.TestCase):
    """release.sh reads the constant and derives everything from it.

    Asserted against the script's text: running it for real would clone
    and push. What each assertion protects is a property the script's
    header states, and losing any one of them puts a wrong number on a
    published release.
    """

    @classmethod
    def script(cls):
        return _read("scripts", "release.sh")

    def test_the_tag_is_read_from_the_staged_snapshot_not_the_working_tree(self):
        # The tag has to name what is actually being published: the
        # staged tree is `git archive HEAD`, so uncommitted edits to
        # version.py cannot end up naming a release.
        self.assertIn(
            'RELEASE_VERSION="$(cd "$STAGING_DIR" && python3 -c '
            "'import version; print(version.__version__)'",
            self.script())
        self.assertIn('RELEASE_TAG="v$RELEASE_VERSION"', self.script())

    def test_it_refuses_a_version_whose_tag_is_already_published(self):
        script = self.script()
        self.assertIn('rev-parse -q --verify "refs/tags/$RELEASE_TAG"', script)
        self.assertIn("REFUSING to release -- $RELEASE_TAG is already published", script)

    def test_the_annotated_tag_carries_the_release_identity_not_the_maintainers(self):
        # An annotated tag records a TAGGER of its own -- without the
        # -c override it would be the maintainer's real name and email,
        # published next to a commit carefully authored not to be.
        script = self.script()
        self.assertIn(
            'git -C "$STAGING_DIR" \\\n'
            '  -c user.name="$RELEASE_AUTHOR_NAME" -c user.email="$RELEASE_AUTHOR_EMAIL" \\\n'
            '  tag -a "$RELEASE_TAG"',
            script)

    def test_the_branch_and_its_tag_are_pushed_atomically(self):
        # Separately, a tag push failing after the branch push succeeded
        # would leave a published release no tag names -- and the next
        # run would see identical content, say "nothing to release", and
        # never tag it.
        self.assertIn(
            'push --atomic origin \\\n'
            '     "HEAD:refs/heads/$RELEASE_BRANCH" "refs/tags/$RELEASE_TAG"',
            self.script())

    def test_the_gate_refuses_a_release_with_no_changelog_entry(self):
        script = self.script()
        self.assertIn('if [ ! -f "$STAGING_DIR/CHANGELOG.md" ]; then', script)
        self.assertIn("CHANGELOG.md has no '## $RELEASE_TAG' section", script)

    def test_unchanged_content_is_still_nothing_to_release_not_a_collision(self):
        # Ordering, stated as an assertion: the "nothing changed" exit
        # must come BEFORE the tag check, or re-running a no-op release
        # would report a tag collision instead of exiting 0.
        script = self.script()
        self.assertLess(script.index("nothing changed since the last release"),
                        script.index('RELEASE_TAG="v$RELEASE_VERSION"'))


if __name__ == "__main__":
    unittest.main()
