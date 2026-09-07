"""docs/operations.md's hand-kept lists, pinned to the code that owns them.

Task-145 re-checked that chapter claim by claim, and the drift it found
was all of one shape: a list of names copied into prose, with nothing
connecting the copy to the original. The integration tier's paragraph
still named the set of tools that tier needed before the release gate
started running it, so `node` -- required by
`tests_integration/test_release_integration.py` since task-124 -- was
missing; the scan's allowlist enumeration had not grown the reserved-TLD
entry added to the script beside it.

This is the same protection task-131 gave docs/architecture.md's module
table, for the same reason: where this repo keeps a list by hand, a test
keeps the list honest. The NAMES only, checked inside the section that
claims them -- nothing here reads the prose around them, and nothing
here is a docs linter.
"""

import importlib.util
import glob
import os
import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DOC = REPO_ROOT / "docs" / "operations.md"
RELEASE_SH = REPO_ROOT / "scripts" / "release.sh"
INTEGRATION_DIR = REPO_ROOT / "tests_integration"

# scripts/ is not a package (release.sh runs the file by path), so the
# scan is loaded the same way tests/test_scan_release.py loads it.
_spec = importlib.util.spec_from_file_location(
    "scan_release", REPO_ROOT / "scripts" / "scan_release.py")
scan_release = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(scan_release)

#: How many entries docs/operations.md's "The secret and identity scan"
#: section enumerates one by one. A count, deliberately, and only here:
#: the doc names each entry in prose, so the only cheap way to notice a
#: fifth one is to notice that there are five. Bump this when the doc's
#: sentence grows the new entry -- not before.
ALLOWLIST_ENTRIES_ENUMERATED = 4


def _doc_text():
    with open(DOC, "r", encoding="utf-8") as f:
        return f.read()


def _section(text, heading):
    """The body of one `#`-headed section of the document, ending at the
    next heading of the same level or higher. Sections, not the whole
    file: a name that appears somewhere else in the chapter is not the
    same claim, and matching it there is how a list stays stale."""
    level = len(heading) - len(heading.lstrip("#"))
    lines = text.splitlines()
    try:
        start = lines.index(heading)
    except ValueError:  # pragma: no cover - a renamed heading, reported below
        raise AssertionError(
            "docs/operations.md has no %r heading any more; this test reads "
            "that section, so point it at the new one" % heading)
    body = []
    for line in lines[start + 1:]:
        if line.startswith("#") and len(line) - len(line.lstrip("#")) <= level:
            break
        body.append(line)
    return "\n".join(body)


class ReleaseMachinePrerequisitesTests(unittest.TestCase):
    """The release machine's own tool list (task-124, task-130).

    `scripts/release.sh` checks these up front and exits 3 for a missing
    one, so the list is the difference between "this machine cannot run
    a release" and "this snapshot is bad" -- and it grew twice (`node`,
    then `tmux`) for reasons the chapter explains at length.
    """

    def _tools_release_sh_requires(self):
        with open(RELEASE_SH, "r", encoding="utf-8") as f:
            match = re.search(r"^\s*for tool in ([^;]+);\s*do\s*$", f.read(), re.M)
        self.assertIsNotNone(
            match,
            "scripts/release.sh no longer has a `for tool in ...; do` "
            "prerequisite loop; this test reads that loop to find the list "
            "docs/operations.md claims")
        return match.group(1).split()

    def test_every_prerequisite_is_named_in_the_releasing_section(self):
        tools = self._tools_release_sh_requires()
        self.assertGreaterEqual(len(tools), 4, tools)  # not vacuous
        section = _section(_doc_text(), "## Releasing")
        missing = [t for t in tools if "`%s`" % t not in section]
        self.assertEqual(
            missing, [],
            "docs/operations.md's Releasing section does not name these "
            "release-machine prerequisites: %s -- release.sh exits 3 without "
            "them, so the chapter's list has to say so (task-145)"
            % ", ".join(missing))


class IntegrationTierToolsTests(unittest.TestCase):
    """The integration tier's tool list.

    Every module there is gated on the tools it needs with
    `@base.require_tools(...)` and skips without them (fails, inside a
    release), so this is the list a reader needs before running the
    tier at all.
    """

    # python3 is what RUNS the tier rather than something it looks up;
    # naming it as a PATH prerequisite of the tier would be noise, and
    # the Releasing section already names it where it does matter.
    NOT_A_TIER_PREREQUISITE = {"python3"}

    def _tools_the_tier_requires(self):
        tools = set()
        for path in glob.glob(os.path.join(INTEGRATION_DIR, "test_*.py")):
            with open(path, "r", encoding="utf-8") as f:
                for args in re.findall(r"@base\.require_tools\(([^)]*)\)", f.read()):
                    tools.update(re.findall(r"[\"']([^\"']+)[\"']", args))
        return tools - self.NOT_A_TIER_PREREQUISITE

    def test_every_required_tool_is_named_in_the_tier_section(self):
        tools = self._tools_the_tier_requires()
        self.assertGreaterEqual(len(tools), 3, sorted(tools))  # not vacuous
        section = _section(_doc_text(), "### The integration tier")
        missing = sorted(t for t in tools if "`%s`" % t not in section)
        self.assertEqual(
            missing, [],
            "docs/operations.md's integration-tier section does not name "
            "these tools, which %s's modules require on PATH: %s (task-145)"
            % (INTEGRATION_DIR.name, ", ".join(missing)))


class ScanAllowlistEnumerationTests(unittest.TestCase):
    """The scan's allowlist is enumerated in the chapter, entry by entry."""

    def test_the_documented_enumeration_still_covers_every_entry(self):
        self.assertEqual(
            len(scan_release.ALLOWLIST), ALLOWLIST_ENTRIES_ENUMERATED,
            "scripts/scan_release.py's ALLOWLIST has %d entries, but "
            "docs/operations.md's \"The secret and identity scan\" section "
            "enumerates %d of them by hand (\"Today that is ...\"). Add the "
            "new one to that sentence -- an allowlist a reader cannot audit "
            "is the thing that section exists to prevent -- then update "
            "ALLOWLIST_ENTRIES_ENUMERATED here (task-145)."
            % (len(scan_release.ALLOWLIST), ALLOWLIST_ENTRIES_ENUMERATED))


if __name__ == "__main__":
    unittest.main()
