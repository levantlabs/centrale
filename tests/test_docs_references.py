"""Every test file this repository's prose points a reader at exists.

Task-163. `version.py`'s docstring sent the reader to
`tests/test_baseline.py` for the check that keeps the README's stated
`backlog` baseline honest -- a file that has never existed anywhere in
this tree. The test it meant is `tests/test_version.py`, and the citation
was wrong from the day it was written, in a module that ships in the
public snapshot.

Nothing here could catch that. The censuses in
`tests/test_source_contract.py` walk the other direction -- every module
that exists is named in the layout table -- so a name that matches no
module is invisible to them, and prose is not otherwise checked.

The rule is deliberately narrow, and it is not a docs linter: where a
document or a top-level module names a path under `tests/` or
`tests_integration/`, that path has to resolve to a file. Those two
directories are what citations point at, and what renames move; a wrong
one sends a reader looking for the proof of a claim and finding nothing.
Nothing here reads the sentence around the citation.

The test tiers themselves are outside the scan on purpose: they build
fixture trees with invented paths (`tests/test_x.py`, written into a
sandbox by `test_scan_release.py`), and those are inputs to a test, not
citations to follow.
"""

import glob
import os
import re
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: A path under one of the two test directories, as prose writes it:
#: `tests/test_version.py`, tests_integration/base.py, (tests/README.md).
CITATION = re.compile(r"\b(?:tests|tests_integration)/[A-Za-z0-9_./-]+\.(?:py|md)\b")


def _scanned_files():
    """The documents and modules a reader follows a citation out of.

    Everything published as prose (the root markdown and `docs/`), the
    top-level modules whose docstrings and comments cite tests the same
    way, and the scripts a release runs. Not `tests/` or
    `tests_integration/`: see the module docstring.
    """
    patterns = ("*.md", "docs/*.md", "*.py", "scripts/*.py", "scripts/*.sh",
                "tests_integration/README.md")
    paths = []
    for pattern in patterns:
        paths.extend(glob.glob(os.path.join(REPO_ROOT, pattern)))
    return sorted(set(paths))


class CitedTestFilesExistTests(unittest.TestCase):
    def test_every_cited_test_path_resolves(self):
        dangling = []
        cited = 0
        for path in _scanned_files():
            with open(path, "r", encoding="utf-8") as f:
                lines = f.read().splitlines()
            for number, line in enumerate(lines, 1):
                for match in CITATION.findall(line):
                    cited += 1
                    if not os.path.isfile(os.path.join(REPO_ROOT, match)):
                        dangling.append(
                            "%s:%d cites %s"
                            % (os.path.relpath(path, REPO_ROOT), number, match))
        self.assertGreater(cited, 15, cited)  # not vacuous
        self.assertEqual(
            dangling, [],
            "these citations name a test file that does not exist:\n  %s\n"
            "Point each at the file that actually holds the claim, or drop "
            "the citation -- a reader who follows one and finds nothing "
            "cannot tell whether the test was renamed or never written "
            "(task-163)" % "\n  ".join(dangling))

    def test_the_scan_covers_the_documents_that_carry_citations(self):
        # A guard on the guard: the scan is a glob, so a moved document
        # would quietly leave the set rather than fail. These are
        # where the citations live today -- the public prose, and the
        # module whose wrong citation started this.
        scanned = {os.path.relpath(p, REPO_ROOT) for p in _scanned_files()}
        for expected in ("version.py", "CONTRIBUTING.md", "README.md",
                         "docs/architecture.md", "docs/operations.md"):
            self.assertIn(expected, scanned)


if __name__ == "__main__":
    unittest.main()
