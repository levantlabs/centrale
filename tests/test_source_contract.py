"""Tests for the source-text tier's own machinery, and its ratchet.

Task-108 found ~14% of the suite asserting on frontend SOURCE TEXT, in a
style that failed as a `ValueError` out of `setUpClass` when a function
was renamed -- an ERROR on every test in the class, naming neither the
file nor the invariant. This module holds the two things that keep that
from coming back:

- the helpers in `tests/source_contract.py` fail the way they promise --
  as a FAILURE, inside the test that read the slice, naming the file,
  the marker and the invariant;
- no test fixture anywhere under `tests/` resolves a source slice, so no
  substring can move and error a whole class again.

Plus the census the task's fourth acceptance criterion asks for, as a
ceiling rather than a snapshot: source-text tests are legitimate for a
source-SHAPE invariant, and the ceiling is what stops the next feature
from adding fifteen more out of habit.
"""

import ast
import glob
import json
import os
import re
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import source_contract  # noqa: E402
from source_contract import (  # noqa: E402
    FRONTEND_FILES, SourceLookupError, function_body, load_static, region,
    to_end_of_code,
)

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
ARCHITECTURE_DOC = os.path.join(
    os.path.dirname(TESTS_DIR), "docs", "architecture.md")


def _architecture_doc():
    with open(ARCHITECTURE_DOC, "r", encoding="utf-8") as f:
        return f.read()


#: The number of source-text-asserting tests left across
#: `tests/test_server.py` and `tests/test_frontend_behaviour.py`: 148
#: when task-108 started, 138 when it stopped, and 98 after task-118
#: converted the six families task-108 had listed as driveable and left
#: alone (the milestone pair, external work, external merge, the agent
#: badge's session scope and the drawer's wide mode -- 53 tests, of
#: which 13 survive as source-SHAPE residue). Four of the 98 are
#: HttpApiTests reading a static file as the expected BODY of an HTTP
#: response rather than as source, so the contract-test count proper is
#: four lower at either end.
#:
#: Lower it when a source-text test is retired; think hard before
#: raising it. A behavioural claim ("there is one poller", "opening the
#: theater fetches nothing") belongs in tests/test_frontend_behaviour.py,
#: where it is driven; a source-SHAPE claim ("the pane fetch has one
#: home", "the seam publishes no name twice") is what this tier is for.
#:
#: Task-107 added three source-text claims the behavioural tier cannot
#: make: the footer's version credit is declared once in the shell, it
#: shares a flex child with the settings gear (a third top-level child
#: of a `space-between` footer would be centred instead of sitting
#: beside the gear), and it is styled as a credit rather than a
#: control. The first two are markup the shim never builds -- it reads
#: index.html for ids only, not for structure -- and the third is CSS,
#: which nothing here renders. What the credit SHOWS, and when, is
#: driven under node in VersionCreditBehaviourTests instead. So the
#: ceiling is 98 + 3.
#:
#: Task-126 added two more, both cross-file COUNTS rather than claims
#: about behaviour: that the drawer's open and its refresh still share
#: one `fetch("/api/task?` call site and one caller of
#: `C.refreshDrawerDetail()` across every frontend file, and that the
#: two pieces of refresh state are declared on the namespace and written
#: from nowhere but state.js and drawer.js. A driver walks one drawer;
#: neither of those is visible from inside one. Everything task-126
#: actually does to that drawer -- including the timer it does not arm,
#: which this tier would have grepped for before -- is driven in
#: DrawerBodyRefreshBehaviourTests. So the ceiling is 101 + 2.
#:
#: Task-98 added three for the header's two links -- the product name
#: pointing at Centrale's own repository, and the Backlog.md credit
#: beside it, which had shipped with no test at all. Both are inert
#: anchors in index.html styled by styles.css, with no JavaScript
#: anywhere near them: the behavioural tier builds no document shell
#: and renders no stylesheet, so a wrong URL, a missing rel, or a
#: title restyled into a button are all invisible to it. There is no
#: behaviour here to drive. So the ceiling is 103 + 3.
SOURCE_TEXT_CEILING = 106

CENSUS_MODULES = ("test_server.py", "test_frontend_behaviour.py")


class SourceRegionFailureTests(unittest.TestCase):
    """A marker that moved must fail readably, and never in a fixture."""

    def setUp(self):
        self.drawer = load_static("drawer.js")

    def test_a_region_resolves_nothing_until_a_test_reads_it(self):
        # The whole point: building the slice cannot fail, so a fixture
        # that builds ten of them cannot error the class.
        gone = region(self.drawer, "function thisWasRenamedAwayInTheFuture(",
                      invariant="a marker that is not there")
        self.assertIsInstance(gone, source_contract.SourceRegion)
        with self.assertRaises(SourceLookupError) as caught:
            "anything" in gone
        message = str(caught.exception)
        self.assertIn("static/drawer.js", message)
        self.assertIn("thisWasRenamedAwayInTheFuture", message)
        self.assertIn("a marker that is not there", message)
        self.assertIn("rename", message)

    def test_the_failure_is_a_failure_not_an_error(self):
        # unittest reports self.failureException as a FAILURE; anything
        # else is an error. SourceLookupError is an AssertionError so a
        # renamed function reads as "this test's claim no longer holds".
        self.assertTrue(issubclass(SourceLookupError, AssertionError))
        self.assertTrue(issubclass(SourceLookupError, self.failureException))

    def test_a_missing_end_marker_names_the_invariant_too(self):
        gone = region(self.drawer, "function renderDrawerDetail(",
                      "function thisIsNotAFunction(",
                      invariant="the drawer's detail body")
        with self.assertRaises(SourceLookupError) as caught:
            len(gone)
        self.assertIn("thisIsNotAFunction", str(caught.exception))
        self.assertIn("the drawer's detail body", str(caught.exception))

    def test_index_on_a_region_fails_readably_instead_of_raising_value_error(self):
        detail = region(self.drawer, "function renderDrawerDetail(",
                        "// Acceptance criteria", invariant="the drawer's detail body")
        with self.assertRaises(SourceLookupError) as caught:
            detail.index("nothing spells this")
        self.assertIn("nothing spells this", str(caught.exception))
        self.assertIn("the drawer's detail body", str(caught.exception))

    def test_index_on_a_whole_file_names_the_file(self):
        with self.assertRaises(SourceLookupError) as caught:
            self.drawer.index("nothing spells this either")
        self.assertIn("static/drawer.js", str(caught.exception))

    def test_a_region_behaves_like_the_string_it_replaced(self):
        detail = region(self.drawer, "function renderDrawerDetail(",
                        "// Acceptance criteria", invariant="the drawer's detail body")
        self.assertIn("function renderDrawerDetail(", detail)
        self.assertNotIn("// Acceptance criteria", detail)
        self.assertGreater(len(detail), 100)
        self.assertEqual(detail[:9], "function ")
        self.assertEqual(detail.count("function renderDrawerDetail("), 1)
        self.assertTrue(str(detail).startswith("function renderDrawerDetail("))

    def test_function_body_stops_at_the_top_level_brace(self):
        body = function_body(load_static("state.js"), "persistDrawerWide")
        self.assertIn("function persistDrawerWide(", body)
        self.assertTrue(str(body).rstrip().endswith("}"))
        self.assertEqual(body.count("function "), 1)

    def test_to_end_of_code_stops_at_the_seam_block(self):
        tail = to_end_of_code(load_static("drawer.js"), "function renderDrawerHarvestArea()",
                              invariant="the drawer's harvest area")
        self.assertIn("function renderDrawerHarvestArea()", tail)
        self.assertNotIn("Seam: what the other files reach for", tail)


class NoFixtureResolvesSourceTests(unittest.TestCase):
    """The structural half of task-108's AC #3.

    `DrawerSpawnAreaJustMergedTests` used to slice `drawer.js` in
    `setUp`, so renaming `renderDrawerSpawnArea` raised
    `ValueError: substring not found` before any test ran. Nothing in
    `tests/` may do that again: a fixture builds regions, it does not
    resolve them.
    """

    def _fixtures(self):
        for path in sorted(glob.glob(os.path.join(TESTS_DIR, "*.py"))):
            with open(path, "r", encoding="utf-8") as f:
                tree = ast.parse(f.read())
            for cls in tree.body:
                if not isinstance(cls, ast.ClassDef):
                    continue
                for fn in cls.body:
                    if isinstance(fn, ast.FunctionDef) and fn.name in ("setUp", "setUpClass"):
                        yield os.path.basename(path), cls.name, fn

    def test_no_setup_searches_a_source_file_for_a_marker(self):
        offenders = []
        for module, cls_name, fn in self._fixtures():
            for node in ast.walk(fn):
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                        and node.func.attr in ("index", "rindex")):
                    offenders.append("%s: %s.%s -- %s"
                                     % (module, cls_name, fn.name, ast.unparse(node)))
        self.assertEqual(
            offenders, [],
            "a fixture that locates a marker fails as an ERROR out of setUp when the "
            "marker moves, taking every test in the class with it. Build the slice with "
            "source_contract.region()/function_body() instead: it resolves inside the "
            "test that reads it. Offenders:\n  " + "\n  ".join(offenders))

    def test_the_fixtures_that_read_source_still_do_so_lazily(self):
        # A sanity check on the check: several classes DO build regions
        # in setUpClass, which is fine and is what the rule permits.
        builders = [
            "%s.%s" % (cls_name, fn.name)
            for _, cls_name, fn in self._fixtures()
            if {"region", "function_body", "to_end_of_code", "js_function",
                "js_to_end_of_code"} & {
                    n.id for n in ast.walk(fn) if isinstance(n, ast.Name)}
        ]
        self.assertGreater(len(builders), 10, builders)


class SourceTextCensusTests(unittest.TestCase):
    """AC #4: the count is measured, and it is a ceiling."""

    def _census(self):
        totals = {"total": 0, "source_text": 0, "behaviour": 0}
        for name in CENSUS_MODULES:
            counts = source_contract.census(os.path.join(TESTS_DIR, name))
            for key in totals:
                totals[key] += counts[key]
        return totals

    def test_source_text_tests_do_not_outgrow_the_ceiling(self):
        counts = self._census()
        self.assertLessEqual(
            counts["source_text"], SOURCE_TEXT_CEILING,
            "%d tests now assert on frontend source text, above the %d task-118 left "
            "behind. If the new one is a claim about what the frontend DOES, drive it "
            "in tests/test_frontend_behaviour.py instead; if it really is a claim about "
            "the shape of the source, raise SOURCE_TEXT_CEILING and say why."
            % (counts["source_text"], SOURCE_TEXT_CEILING))

    def test_the_behavioural_tier_is_not_empty(self):
        # The other side of the ratchet: source-text tests may only be
        # retired because something drives the claim instead.
        self.assertGreaterEqual(self._census()["behaviour"], 76)


class DocumentedCeilingTests(unittest.TestCase):
    """docs/architecture.md quotes the ceiling this module enforces.

    Task-163. The chapter's Testing section carries the current figure --
    "it caps how many source-text tests the suite has (106 today -- 148
    before task-108 ...)" -- and by the time anyone read it against the
    constant it had been wrong twice: task-141 found it saying 101 while
    the ceiling was 103, and task-98's raise to 106 left it saying 103.
    Both times the raise WAS the edit, and the prose was a second place
    to remember. So the figure is pinned rather than maintained. Only the
    figure: the history it walks is hand-written and stays that way,
    because a step that already happened cannot go stale.
    """

    QUOTED_CEILING = re.compile(r"source-text tests the suite has \((\d+) today")

    def test_the_testing_chapter_quotes_the_enforced_ceiling(self):
        match = self.QUOTED_CEILING.search(_architecture_doc())
        self.assertIsNotNone(
            match,
            "docs/architecture.md no longer says how many source-text tests "
            "the suite has, in the form \"... the suite has (N today -- ...)\". "
            "This test reads that phrase to check its number against "
            "SOURCE_TEXT_CEILING; if the sentence was reworded, point this at "
            "the new wording rather than dropping the pin (task-163)")
        self.assertEqual(
            int(match.group(1)), SOURCE_TEXT_CEILING,
            "docs/architecture.md says the source-text tier is capped at %s "
            "tests; SOURCE_TEXT_CEILING here is %d. The constant is what the "
            "suite enforces, the doc is what a reader believes -- so a raise "
            "is one edit in two places: the ceiling, and that sentence's "
            "figure plus the history step explaining it (task-163)"
            % (match.group(1), SOURCE_TEXT_CEILING))


class TestModulesAreDocumentedTests(unittest.TestCase):
    """docs/architecture.md's layout table names every module in tests/.

    Task-131. The table is a hand-kept list, and task-104 had already
    corrected it once when seven tasks in a row each added a module
    without visiting it: a day later it named six of thirteen. Where this
    repo keeps a list by hand it pins the list with a test (the ratchet
    above, docs/api.md's field lists), so this is the same protection for
    the one claim the table makes about tests/: a new `tests/test_*.py`
    fails the suite until the doc names it. That one claim, and nothing
    broader -- this is not a docs linter, and the phrase the table gives
    each module is not checked, only the name.
    """

    def test_every_test_module_is_named_in_the_layout_table(self):
        text = _architecture_doc()
        modules = sorted(os.path.basename(p)
                         for p in glob.glob(os.path.join(TESTS_DIR, "test_*.py")))
        self.assertGreater(len(modules), 10, modules)  # not vacuous
        missing = [m for m in modules if "`%s`" % m not in text]
        self.assertEqual(
            missing, [],
            "docs/architecture.md's repository-layout table does not name "
            "these test modules: %s -- add each to the `tests/` row with a "
            "phrase saying what it covers (task-131)" % ", ".join(missing))


class TopLevelModulesAreDocumentedTests(unittest.TestCase):
    """The same table names every top-level `*.py` module, one row each.

    Task-150. `centrale_notify.py` had never been a row -- verified with
    `git log -S`, so an omission from the start rather than drift -- while
    other rows in the same table referred to it freely. Task-131 pinned the
    table's claim about `tests/`, and the omission survived precisely
    because that pin looks at nothing else.

    So widen it here, in the same census shape and with the same narrow
    claim: the name has a row, not what the row says about it. A row for a
    top-level module is the cheap half to pin, because the set is a glob;
    `scripts/` and `tests_integration/` stay hand-kept single rows for a
    DIRECTORY, whose membership the table deliberately does not enumerate
    (see the `scripts/` row, and tests_integration/README.md).

    Scoped to the table's rows, not to the whole chapter, unlike the
    task-131 census above: every one of these modules also has a `###`
    section further down, so a substring search over the text would pass
    for a module the table never lists.
    """

    #: The layout table's rows are the lines that open with a cell holding
    #: exactly one backticked path.
    ROW = re.compile(r"^\| `([^`|]+)` \|", re.MULTILINE)

    def test_every_top_level_module_has_a_row_in_the_layout_table(self):
        repo_root = os.path.dirname(TESTS_DIR)
        modules = sorted(os.path.basename(p)
                         for p in glob.glob(os.path.join(repo_root, "*.py")))
        self.assertGreater(len(modules), 5, modules)  # not vacuous

        section = _architecture_doc().split("## Module layout", 1)
        self.assertEqual(len(section), 2,
                         "docs/architecture.md has no '## Module layout' "
                         "heading for the layout table to live under")
        rows = set(self.ROW.findall(section[1].split("\n## ", 1)[0]))
        self.assertIn("server.py", rows, sorted(rows))  # the regex still matches

        missing = [m for m in modules if m not in rows]
        self.assertEqual(
            missing, [],
            "docs/architecture.md's module-layout table has no row for these "
            "top-level modules: %s -- give each its own row saying what it is "
            "for (task-150). A mention inside another row's prose is not a "
            "row: `centrale_notify.py` had several of those and no row"
            % ", ".join(missing))


class FrontendFilesAreDocumentedTests(unittest.TestCase):
    """docs/architecture.md's frontend table is the load order, in order.

    Task-141. "The frontend files" is a hand-kept table of the
    `static/*.js` files and what each is for, and the paragraph under it
    turns on the order being the load order -- which file may run
    statements as it loads, and why main.js is last. The order and the
    membership are already pinned against index.html and the static
    directory (StaticSplitContractTests, through FRONTEND_FILES), so the
    one place they can still drift is the doc, exactly as the tests/ row
    had drifted to six of thirteen before task-131 pinned it.

    Same shape as that census, and the same narrow claim: the names the
    table lists, and their order. What the table says each file is FOR is
    prose and is not checked -- this is not a docs linter.
    """

    #: The table's rows are the only lines in the chapter that open with
    #: a cell holding exactly one backticked `*.js` name.
    ROW = re.compile(r"^\| `([A-Za-z0-9_.-]+\.js)` \|", re.MULTILINE)

    def test_the_frontend_table_lists_every_file_in_load_order(self):
        section = _architecture_doc().split("### The frontend files", 1)
        self.assertEqual(len(section), 2,
                         "docs/architecture.md has no '### The frontend files' "
                         "section for the frontend table to live in")
        # Stop at the next heading, so a `*.js` row in a later section
        # cannot be mistaken for part of this table.
        table = section[1].split("\n### ", 1)[0]
        listed = self.ROW.findall(table)
        self.assertEqual(
            listed, list(FRONTEND_FILES),
            "docs/architecture.md's 'The frontend files' table lists %r, but "
            "index.html loads %r (tests/source_contract.py's FRONTEND_FILES). "
            "Update the table -- one row per file, in load order -- and the "
            "sentence above it that counts them (task-141)."
            % (listed, list(FRONTEND_FILES)))


class AgentsDocQuotesTheCodeTests(unittest.TestCase):
    """docs/agents.md quotes spawn.py verbatim in three places.

    Task-139 verified that chapter against the code claim by claim.
    Three of its claims are the kind that go stale in silence: nothing
    breaks when the prompt every agent is handed, the geometry every
    session is born with, or the argv a Resume launches drifts away from
    the sentence describing it, and a reader has no way to tell. So they
    are pinned here, the same way task-131 pinned
    docs/architecture.md's module list -- read out of spawn.py itself,
    never restated, so the doc is what fails when the code moves.

    Deliberately narrow: three quoted values, not a docs linter. The
    prose around them is a human's job to keep true.
    """

    DOC = os.path.join(os.path.dirname(TESTS_DIR), "docs", "agents.md")

    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, os.path.dirname(TESTS_DIR))
        with open(cls.DOC, "r", encoding="utf-8") as f:
            cls.text = f.read()

    def test_the_quoted_spawn_prompt_is_the_prompt_agents_are_given(self):
        import spawn

        # The doc renders the template as a blockquote: "> " margins,
        # its own line wrapping, {task_id} shown as \<ID> so the
        # placeholder survives Markdown, and "--" typeset as an em dash.
        # Those are presentation; the words are the claim.
        block = re.search(r"(?:^> .*\n)+", self.text, re.M)
        self.assertIsNotNone(block, "docs/agents.md no longer blockquotes the spawn prompt")
        quoted = " ".join(line[2:].strip() for line in block.group(0).strip().splitlines())
        quoted = re.sub(r"\s+", " ", quoted).replace("\\<ID>", "{task_id}").replace("\u2014", "--")
        self.assertEqual(
            quoted, re.sub(r"\s+", " ", spawn.PROMPT_TEMPLATE),
            "docs/agents.md's quoted spawn prompt is no longer spawn.PROMPT_TEMPLATE -- "
            "requote it (task-139)")

    def test_the_documented_session_geometry_is_the_one_sessions_get(self):
        import spawn

        columns, rows = spawn.SESSION_GEOMETRY
        phrase = "%d columns by %d rows" % (columns, rows)
        self.assertIn(
            phrase, self.text,
            "docs/agents.md does not say sessions are created %s -- spawn.SESSION_GEOMETRY "
            "changed and the chapter still names the old size (task-139)" % phrase)

    def test_every_resume_family_default_is_named_in_the_doc(self):
        """The resume tiers' family defaults, read out of spawn.resume().

        Tier 2 is a chain of literals inside that function (task-115
        added the codex one beside claude's), and the doc lists them
        by hand. Collecting the `cmd = [...]` literals the function
        assigns is enough to notice a new family, or a changed flag,
        that the list was never told about.
        """
        import spawn

        with open(spawn.__file__, "r", encoding="utf-8") as f:
            source = f.read()
        resume = next(
            node for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.FunctionDef) and node.name == "resume")
        defaults = [
            [element.value for element in node.value.elts]
            for node in ast.walk(resume)
            if isinstance(node, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "cmd" for t in node.targets)
            and isinstance(node.value, ast.List)
            and node.value.elts
            and all(isinstance(e, ast.Constant) and isinstance(e.value, str)
                    for e in node.value.elts)
        ]
        self.assertGreaterEqual(len(defaults), 2, defaults)  # not vacuous
        missing = [d for d in defaults if json.dumps(d) not in self.text]
        self.assertEqual(
            missing, [],
            "spawn.resume()'s family defaults %s are not named in docs/agents.md's "
            "resume tiers -- add each as a `%s`-style entry (task-139)"
            % (missing, json.dumps(["claude", "--continue"])))


#: The predicates docs/board.md's lifecycle map is derived from -- the
#: names the frontend actually decides a task's state with. The map is
#: worthless the moment it stops describing these, so it must keep
#: naming every one of them, and every name it uses must still exist in
#: `static/`. Add to this list when a new predicate joins the state
#: machine; rename here and in the doc when one is renamed in the code.
LIFECYCLE_PREDICATES = (
    "findLiveSession",
    "effectiveHasSpawnBranch",
    "worktreeDirty",
    "branchCheckout",
    "externalCheckout",
    "parkedBranch",
    "alreadyMerged",
    "drawerBranchStatusIsActive",
    "isActiveStatus",
    "endSessionArmReason",
    "liveSessionBlockReason",
)

#: The action labels the map's second and third columns promise, spelled
#: exactly as the buttons spell them. A label the user cannot find on
#: screen is the same failure as a state that no longer exists.
LIFECYCLE_ACTIONS = (
    "Spawn agent",
    "Resume agent",
    "Re-spawn agent",
    "End session",
    "Merge",
    "Merged \u2014 clean up",
    "Discard attempt",
    "Abandon worktree, keep branch",
    "Worked externally",
)

LIFECYCLE_HEADING = "## The task lifecycle"


class LifecycleMapIsPinnedToTheCodeTests(unittest.TestCase):
    """docs/board.md's lifecycle map still describes the real predicates.

    Task-138. The map is a hand-drawn state diagram plus an actions
    table, and a hand-drawn lifecycle rots faster than prose because
    nothing recomputes it: the states it names are `findLiveSession`,
    `worktreeDirty`, `drawerBranchStatusIsActive` and friends, three of
    which changed meaning in the same day the doc was written (task-116
    widened Resume, task-119 added the throwaway actions, task-120 made
    End session unconditional). Same protection task-131 gave
    architecture.md's module list: where this repo keeps a list by hand
    it pins the list with a test.

    Deliberately narrow, and deliberately not a docs linter -- it pins
    the VOCABULARY, not the prose. A predicate the doc names must still
    exist in `static/`; a predicate the states are derived from must
    still be named; an action label must still be spelled the way the
    button spells it. What the doc SAYS about any of them is a human's
    job to keep true.

    Lives here rather than in test_server.py on purpose: this module is
    outside the source-text census above, and this is a claim about a
    DOC, not about the shape of the frontend.
    """

    def _doc(self):
        path = os.path.join(os.path.dirname(TESTS_DIR), "docs", "board.md")
        with open(path, "r", encoding="utf-8") as f:
            return f.read()

    def _section(self):
        doc = self._doc()
        self.assertIn(
            LIFECYCLE_HEADING, doc,
            "docs/board.md has lost its %r section -- the lifecycle map is the "
            "page's orientation and belongs at the top of it (task-138)"
            % LIFECYCLE_HEADING)
        after = doc.split(LIFECYCLE_HEADING, 1)[1]
        return after.split("\n## ", 1)[0]

    def _frontend(self):
        return "".join(str(load_static(name))
                       for name in source_contract.FRONTEND_FILES)

    def test_the_page_opens_with_the_map_and_the_map_opens_with_a_diagram(self):
        doc = self._doc()
        first = doc.index("\n## ") + 1
        self.assertTrue(
            doc[first:].startswith(LIFECYCLE_HEADING),
            "the lifecycle map must be docs/board.md's FIRST section: the page "
            "opening straight into 'Using the board' with no orientation is the "
            "gap task-138 exists to close. First section found: %r"
            % doc[first:doc.index("\n", first)])
        self.assertIn(
            "```mermaid", self._section(),
            "the map's diagram is a ```mermaid fence -- GitHub renders it on the "
            "repo page with no image asset to maintain (task-138)")
        self.assertIn("stateDiagram", self._section())

    def test_the_map_names_every_predicate_its_states_come_from(self):
        section = self._section()
        missing = [p for p in LIFECYCLE_PREDICATES if "`%s`" % p not in section]
        self.assertEqual(
            missing, [],
            "docs/board.md's lifecycle map no longer names these predicates: %s "
            "-- the map's whole claim is that its states are the ones the "
            "frontend decides with, so each must appear in it, in backticks "
            "(task-138)" % ", ".join(missing))

    def test_every_predicate_the_map_names_still_exists(self):
        # Every backticked camelCase identifier in the section, `a.b`
        # split on the dot and a trailing "()" dropped. Fenced blocks are
        # removed first: the diagram is not a set of code spans, and its
        # backticks would misalign the pairing of the ones that are.
        prose = re.sub(r"```.*?```", "", self._section(), flags=re.S)
        named = set()
        for span in re.findall(r"`([^`\n]+)`", prose):
            for part in span.rstrip("()").split("."):
                if re.fullmatch(r"[a-z][A-Za-z0-9_]*", part) and part.lower() != part:
                    named.add(part)
        self.assertGreaterEqual(len(named), len(LIFECYCLE_PREDICATES), sorted(named))
        source = self._frontend()
        missing = sorted(n for n in named if n not in source)
        self.assertEqual(
            missing, [],
            "docs/board.md's lifecycle map names these identifiers, and no file "
            "under static/ contains them any more: %s -- either they were "
            "renamed (update the map, and LIFECYCLE_PREDICATES here) or the map "
            "has drifted from the code (task-138)" % ", ".join(missing))

    def test_every_action_the_map_names_is_still_a_button_label(self):
        section = self._section()
        source = self._frontend()
        undocumented = [a for a in LIFECYCLE_ACTIONS if a not in section]
        self.assertEqual(
            undocumented, [],
            "docs/board.md's lifecycle map no longer names these actions: %s -- "
            "the table's point is which actions a state offers and why the rest "
            "are absent, so every action has to appear in it (task-138)"
            % ", ".join(undocumented))
        renamed = [a for a in LIFECYCLE_ACTIONS if '"%s"' % a not in source]
        self.assertEqual(
            renamed, [],
            "docs/board.md's lifecycle map spells these actions differently from "
            "the buttons: %s is not a label in any static/*.js -- a label the "
            "user cannot find on screen is as wrong as a state that no longer "
            "exists (task-138)" % ", ".join(renamed))


#: Every control label docs/board.md names at the reader, paired with the
#: frontend file that renders it. task-142: the board guide describes the
#: UI in prose, and the one kind of claim in it that goes wrong silently
#: is the NAME of a control -- a rename lands in one file, the sentence
#: that quotes it lands nowhere, and the doc goes on telling the reader
#: to click something that isn't there. The 2026-09-04 audit found
#: exactly that ("Ready only" documented for a switch labelled "Ready to
#: start"), so this pins the labels the same way task-131 pinned
#: architecture.md's module list.
#:
#: Each label has to appear BOTH in `static/<file>` and in
#: docs/board.md, which is what makes it a pin rather than a snapshot: a
#: rename that updates only the code fails here, and so does one that
#: updates only the doc. Nothing else about the sentence around the
#: label is checked -- this is not a docs linter.
#:
#: LIFECYCLE_ACTIONS above covers the nine action labels the lifecycle
#: map promises, inside that section and against every frontend file at
#: once. This list is the rest of the page's vocabulary -- the filters,
#: the badges, the pane and theater controls, the drawer's sections --
#: each pinned to the ONE file that renders it. The two overlap on
#: "Discard attempt" and "Abandon worktree, keep branch", which the
#: prose below the map names too; a duplicated label costs a tuple entry
#: and buys the second half of the claim.
BOARD_DOC_CONTROL_LABELS = (
    ("Ready to start", "index.html"),
    ("Refresh", "index.html"),
    ("Welcome to Centrale", "index.html"),
    ("Open task", "index.html"),
    ("All milestones", "tasks.js"),
    ("ready", "board.js"),
    ("blocked", "board.js"),
    ("unmerged branch", "board.js"),
    ("interrupted", "board.js"),
    (" \u2014 spawn anyway?", "spawn.js"),
    ("On agent branch (unmerged)", "drawer.js"),
    ("Discard attempt", "harvest.js"),
    ("Abandon worktree, keep branch", "harvest.js"),
    ("Live session pane", "pane.js"),
    ("Expand", "pane.js"),
    ("Maximize", "pane.js"),
    ("Hide task", "pane.js"),
    ("Show task", "pane.js"),
    ("Send", "pane.js"),
    ("Esc", "pane.js"),
)


class BoardDocControlLabelsTests(unittest.TestCase):
    """docs/board.md quotes the labels the frontend actually renders."""

    def _doc(self):
        path = os.path.join(os.path.dirname(TESTS_DIR), "docs", "board.md")
        with open(path, "r", encoding="utf-8") as f:
            # The doc wraps its prose, so a two-word label can straddle a
            # line break; the comparison is on one long line instead.
            return " ".join(f.read().split())

    def test_every_documented_control_label_is_the_one_the_code_renders(self):
        missing = []
        for label, filename in BOARD_DOC_CONTROL_LABELS:
            if label not in load_static(filename):
                missing.append("%r (docs/board.md) is not in static/%s"
                               % (label, filename))
        self.assertEqual(
            missing, [],
            "docs/board.md names a control the frontend no longer spells that "
            "way. Rename it in the doc as well as here (task-142):\n  "
            + "\n  ".join(missing))

    def test_the_doc_still_names_every_label_this_pin_covers(self):
        doc = self._doc()
        missing = [label for label, _ in BOARD_DOC_CONTROL_LABELS if label not in doc]
        self.assertEqual(
            missing, [],
            "these labels are pinned as documented in docs/board.md but the doc "
            "no longer mentions them -- either the doc dropped a control it "
            "should still describe, or the pin above is stale: %s"
            % ", ".join(repr(m) for m in missing))


class ConfigurationDocumentedTests(unittest.TestCase):
    """docs/configuration.md names every projects.json key, with its default.

    Task-143, the same protection TestModulesAreDocumentedTests gives
    docs/architecture.md's module list (task-131): the chapter's field
    list is hand-kept, and a key added to load_config without a visit
    here is exactly the silent drift a verification pass has to find by
    reading. Two claims are pinned, and nothing broader -- that every
    key is NAMED, and that the default the bullet quotes is the one the
    code actually falls back to. What each bullet says about the key
    beyond that is prose, and stays a reading job.
    """

    DOC = os.path.join(os.path.dirname(TESTS_DIR), "docs", "configuration.md")

    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, os.path.dirname(TESTS_DIR))
        with open(cls.DOC, "r", encoding="utf-8") as f:
            cls.text = f.read()

    @staticmethod
    def _bullets(text, indent=""):
        """The doc's field bullets at one indent level -- a line opening
        with a dash and the key in backticks -- as {key: the whole
        bullet, including its indented continuation lines}."""
        pattern = re.compile(r"^%s- `([A-Za-z]+)`" % indent)
        bullets, name, buf = {}, None, []
        for line in text.splitlines():
            match = pattern.match(line)
            if match:
                if name:
                    bullets[name] = "\n".join(buf)
                name, buf = match.group(1), [line]
            elif name is not None:
                if line.strip() and not line.startswith(indent + "  "):
                    bullets[name], name, buf = "\n".join(buf), None, []
                else:
                    buf.append(line)
        if name:
            bullets[name] = "\n".join(buf)
        return bullets

    @staticmethod
    def _leaves(value):
        """Every scalar inside a default value, as strings -- what the
        bullet documenting it has to quote somewhere ("click" for
        {"mode": "click"}, "claude"/"codex" for the agents map)."""
        if isinstance(value, dict):
            return [leaf for v in value.values() for leaf in
                    ConfigurationDocumentedTests._leaves(v)]
        if isinstance(value, list):
            return [leaf for v in value for leaf in
                    ConfigurationDocumentedTests._leaves(v)]
        if isinstance(value, bool) or value is None:
            return []
        if isinstance(value, (str, int, float)):
            return [str(value)]
        return []

    def _zero_config(self):
        # No file at all: every value in what comes back IS the default.
        import server

        with tempfile.TemporaryDirectory() as tmp:
            return server.load_config(os.path.join(tmp, "projects.json"))

    def _one_project(self):
        """The project dict load_config builds from an entry that sets
        nothing but name/path -- so every OTHER key on it is a default."""
        import server

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "projects.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"projects": [{"name": "p", "path": tmp}]}, f)
            return server.load_config(path)["projects"][0]

    def test_every_top_level_key_has_a_bullet(self):
        documented = self._bullets(self.text)
        # "zeroConfig" is derived at load time (a missing FILE), not a key
        # anyone writes in projects.json -- see load_config.
        keys = sorted(set(self._zero_config()) - {"zeroConfig"})
        self.assertGreater(len(keys), 8, keys)  # not vacuous
        missing = [k for k in keys if k not in documented]
        self.assertEqual(
            missing, [],
            "docs/configuration.md's Fields list has no `- `<key>`` bullet for these "
            "projects.json keys: %s -- document each one there (task-143)"
            % ", ".join(missing))

    def test_every_project_entry_key_is_documented(self):
        project = self._one_project()
        sub_bullets = self._bullets(self.text, indent="  ")
        top_bullets = self._bullets(self.text)
        # A per-project key is documented either as its own sub-bullet
        # under `projects`, or inside the top-level bullet of the same
        # name where it is an override of a global (worktreeRoot).
        documented = set(sub_bullets) | {
            k for k, body in top_bullets.items() if "per-project" in body}
        missing = sorted(set(project) - documented)
        self.assertEqual(
            missing, [],
            "docs/configuration.md documents no per-project `%s` -- add a sub-bullet "
            "under `projects` (task-143)" % ", ".join(missing))

    def test_documented_defaults_are_the_code_defaults(self):
        documented = self._bullets(self.text)
        config = self._zero_config()
        checked = 0
        for key, default in sorted(config.items()):
            if key == "zeroConfig" or key not in documented:
                continue
            for leaf in self._leaves(default):
                checked += 1
                self.assertIn(
                    leaf, documented[key],
                    "docs/configuration.md's `%s` bullet never mentions %r, which is "
                    "what an omitted `%s` actually falls back to (task-143)"
                    % (key, leaf, key))
        self.assertGreater(checked, 6, "no defaults were checked")

    def test_the_per_project_check_timeout_default_is_documented(self):
        project = self._one_project()
        bullet = self._bullets(self.text, indent="  ")["checkTimeoutSeconds"]
        self.assertIn(
            str(project["checkTimeoutSeconds"]), bullet,
            "docs/configuration.md's `checkTimeoutSeconds` sub-bullet quotes a "
            "default the code no longer uses (task-143)")


if __name__ == "__main__":
    unittest.main()
