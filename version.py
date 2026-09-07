"""Centrale's two version constants -- the single source of truth, and nothing else.

`__version__` below is the ONLY place Centrale's own number is written
down. The release tag, the CHANGELOG heading and what the running server
reports are all derived from it (see docs/operations.md, "Versioning"),
so bumping a release is one edit rather than three that can disagree.

`TESTED_BACKLOG_VERSION` is the second number, and it is here for the
same reason (task-156). Centrale is a veneer over the `backlog` CLI --
every piece of task data it shows came out of that binary -- so every
release is implicitly a claim about which `backlog` it was verified
against. That claim used to live only in README prose, which nothing
checked, on a dependency that shipped 100 releases in its last 355 days
(median gap: ONE day). A statement that goes stale that fast has to be
tied to reality mechanically rather than maintained by discipline, so
this constant is what `--check` reports a user's installed version
against, what `scripts/release.sh` refuses to cut a release without
matching, and what the README's stated version is tested against
(tests/test_version.py).

TESTED, and deliberately no MINIMUM beside it. The two answer different
questions -- "what was this exercised on" versus "below what does this
genuinely not work" -- and only the first has an honest answer today:
nothing here has been run against an older CLI to find the floor, and a
floor nobody measured is exactly the kind of unverified claim this
constant exists to replace. Add MINIMUM when someone has a use for it
and has measured it.

Why its own module rather than a constant in server.py: this file is
read by two very different callers, and it has to be cheap and safe for
both.

  * `scripts/release.sh` reads it out of the STAGED SNAPSHOT with
    `python3 -c "import version; print(version.__version__)"` to derive
    the tag it cuts, and reads `TESTED_BACKLOG_VERSION` the same way to
    check the release machine's own `backlog` against it. That runs
    against a bare tree with nothing configured, so this module
    deliberately imports nothing, touches no filesystem, and has no side
    effects at import: importing it can neither fail for an
    environmental reason nor do anything.
  * `server.py` imports it for the values the board and `--check`
    report.

Nothing here parses prose. The rejected alternative was sourcing the
version from README.md or another human-written file: docs drift (this
repo's own operations.md once claimed 693 tests while the suite ran
911), and the one value that must never lie is the wrong thing to go
looking for in a paragraph.

No packaging metadata comes with this. Centrale is run from a checkout
with `python3 server.py`; there is no `pip install`, no setup.py and no
build step, and a version constant does not need one.
"""

__version__ = "0.1.0"

#: The `backlog` CLI version this release was verified against. Bumping
#: it is a procedure, not an edit -- see docs/operations.md,
#: "Versioning": re-run the suite and a real spawn/merge against the new
#: CLI, update the README's stated version, and say so in the CHANGELOG.
TESTED_BACKLOG_VERSION = "1.51.0"
