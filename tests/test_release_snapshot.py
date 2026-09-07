"""What a public release actually ships.

scripts/release.sh publishes `git archive HEAD` of this repo and nothing
else, so the archive's file list IS the published file list. TASK-101
decided that backlog/ -- this repo's own board, written for the
maintainer and continually re-seeded with filesystem paths and cross-repo
references by the agents working here -- must not ship, and that the
exclusion has to be enforced by the release mechanism rather than by
anyone remembering. The lever is `backlog/ export-ignore` in
.gitattributes; this test is the check that the lever is still connected.

It reads git only (`git archive` against HEAD, no network, no processes
of ours), and skips rather than fails wherever there is no git repo to
read -- most usefully in the staged snapshot release.sh gates, whose HEAD
is the archive's own content and so trivially satisfies the assertion.
"""

import os
import re
import subprocess
import tarfile
import unittest
from io import BytesIO

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Paths that must be in every snapshot. Without these the "no backlog/"
# assertion could pass on an empty or broken archive and prove nothing.
MUST_SHIP = ("server.py", "README.md", "AGENTS.md", "LICENSE",
             "static/index.html",
             # task-107: the version constant and the changelog are the
             # release's own identity -- a snapshot without them is a
             # build nobody can name.
             "version.py", "CHANGELOG.md",
             # task-136: CONTRIBUTING.md is only useful to the people who
             # reach the public repo, so shipping it is the whole point of
             # writing it -- and GitHub surfaces it in the PR and issue UI
             # from the root of the published tree, not from docs/.
             "CONTRIBUTING.md",
             # task-137: the decision records and task descriptions that
             # hold this project's reasoning live in the board, which does
             # not ship. MANIFESTO.md is the only place a public reader
             # gets the "why", so a snapshot without it publishes the
             # what and silently drops the argument.
             "MANIFESTO.md")

# Directories the release must never publish, and why.
MUST_NOT_SHIP_PREFIXES = ("backlog/",)


def _archive_files():
    """Every file `git archive HEAD` would write, as path -> bytes, or
    None if unavailable."""
    try:
        proc = subprocess.run(
            ["git", "-C", REPO_ROOT, "archive", "--format=tar", "HEAD"],
            capture_output=True, timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    with tarfile.open(fileobj=BytesIO(proc.stdout)) as tar:
        return {m.name: tar.extractfile(m).read()
                for m in tar.getmembers() if m.isfile()}


class ReleaseSnapshotContentsTests(unittest.TestCase):
    def setUp(self):
        self.files = _archive_files()
        if self.files is None:
            self.skipTest("no readable git repository here -- nothing to archive")
        self.paths = list(self.files)

    def test_the_snapshot_carries_the_actual_source(self):
        # Guards the exclusion assertion below against passing vacuously.
        for path in MUST_SHIP:
            self.assertIn(path, self.paths)

    def test_the_snapshot_excludes_the_private_board(self):
        leaked = [p for p in self.paths
                  if p.startswith(MUST_NOT_SHIP_PREFIXES)]
        self.assertEqual(
            leaked, [],
            "these would be PUBLISHED by scripts/release.sh: "
            + ", ".join(leaked[:10])
            + " -- check that .gitattributes still marks them export-ignore, "
              "and that .gitattributes itself is committed (git archive reads "
              "attributes from the tree it is archiving, not the working tree)",
        )

    def test_the_snapshot_explains_its_task_id_citations(self):
        # task-111: excluding the board left ~1000 `task-NN` provenance
        # tags in shipped comments and docs pointing at something a public
        # reader cannot open. They stay -- each tags a sentence that
        # carries its own reason -- but the snapshot has to say so
        # somewhere the reader reaches, and README sends them to AGENTS.md.
        self.assertIn("AGENTS.md", self.files)
        agents = self.files["AGENTS.md"].decode("utf-8")
        self.assertIn(
            "task-NN", agents,
            "AGENTS.md is the published tree's only explanation of what a "
            "task-NN citation in a comment refers to -- keep it, or move it "
            "somewhere else that ships and update this test",
        )


    def test_the_snapshot_sends_its_agents_to_the_manifesto(self):
        # task-137: the manifesto only constrains decisions if the file an
        # agent actually opens first points at it. AGENTS.md is that file
        # (CLAUDE.md's first line delegates to it), so the pointer living
        # there is the whole mechanism -- without it MANIFESTO.md is a
        # document that ships and nothing reads.
        agents = self.files["AGENTS.md"].decode("utf-8")
        self.assertIn(
            "MANIFESTO.md", agents,
            "AGENTS.md is what an agent reads before designing anything -- "
            "if it stops naming MANIFESTO.md, the manifesto stops being a "
            "constraint and becomes decoration",
        )


class NoGeneratedContentTests(unittest.TestCase):
    """The snapshot is `git archive HEAD` and nothing more (task-107).

    The rejected alternative for the no-git case (a downloaded zip) was
    stamping a generated version file into the snapshot at release time.
    That would punch a hole in the one property that makes an accidental
    leak structurally hard -- the published tree is byte-identical to
    tracked content at the last commit, so nothing untracked can ride
    along -- and it would add a build step to a project whose whole
    ethos is not having one. The constant already answers the zip case.

    Read from the script's text: running it for real would clone and
    push. What it protects is that the only thing populating the staging
    tree is the archive extraction.
    """

    def setUp(self):
        path = os.path.join(REPO_ROOT, "scripts", "release.sh")
        with open(path, encoding="utf-8") as f:
            self.script = f.read()

    def test_the_staged_tree_is_filled_only_by_extracting_the_archive(self):
        self.assertIn('tar -xf "$ARCHIVE_TAR" -C "$STAGING_DIR"', self.script)
        # Every line that writes INTO the staging tree, other than the
        # extraction (and git's own commands, which write history, not
        # content). A generated file would be one of these.
        #
        # Two shapes: a redirect whose target is under $STAGING_DIR, and
        # a file-writing command given a path under it. A redirect to
        # somewhere else on the same line (`2>/dev/null` on the command
        # that READS the staged version.py) is not a write to the
        # snapshot and must not read as one.
        redirect = re.compile(r'>>?\s*"?\$STAGING_DIR')
        writing_command = re.compile(r'^(cp|mv|tee|install|touch|ln|sed\s+-i)\b')
        writers = []
        for line in self.script.splitlines():
            stripped = line.strip()
            if stripped.startswith("#") or "$STAGING_DIR" not in stripped:
                continue
            if "tar -xf" in stripped:
                continue
            if redirect.search(stripped) or writing_command.match(stripped):
                writers.append(stripped)
        self.assertEqual(
            writers, [],
            "these lines write into the staged snapshot outside 'git archive' -- "
            "a release must publish tracked content and nothing generated:\n  "
            + "\n  ".join(writers))

    def test_the_version_is_read_from_the_snapshot_never_written_into_it(self):
        # The tag comes from the tree being published, not the other way
        # round: nothing stamps a number in.
        self.assertIn(
            "print(version.__version__)", self.script,
            "release.sh derives the tag by READING the staged version.py")


if __name__ == "__main__":
    unittest.main()
