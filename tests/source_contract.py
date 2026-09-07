"""Readable, lazily-resolved slices of the frontend source files.

Centrale's frontend is vanilla JS with no build step and no browser test
dependency, so a large family of tests in `tests/test_server.py` asserts
on the SOURCE of `static/*.js`, `static/index.html` and
`static/styles.css` rather than on behaviour. That style is legitimate
for a genuine source-shape invariant -- "the seam publishes no name
twice", "index.html loads each frontend file once" -- and it is the
wrong tool for a behavioural one, which belongs in
`tests/test_frontend_behaviour.py` where the real sources are driven
under `tests/js_harness.py` (task-108).

What this module fixes is how the textual half FAILS. Before task-108 a
contract class sliced its regions eagerly in `setUpClass`:

    start = js.index("function renderDrawerSpawnArea()")
    self.body = js[start:js.index("function renderDrawerRespawnAction(")]

so renaming that function raised `ValueError: substring not found` out
of the fixture. unittest reports that as an ERROR on every test in the
class, with a message that names neither the file, the marker, nor the
invariant the class exists to protect -- and a safe rename reads like a
crash.

Every slice built here is instead resolved the first time a test
actually reads it, and a marker that has moved raises `AssertionError`
naming the file, the missing text and the invariant. The test FAILS,
inside the test, saying what to look at:

    static/drawer.js: cannot find "function renderDrawerSpawnArea()".
    The source-text invariant "the drawer's spawn area" is asserted
    against the spelling of the source, so this is either a rename
    (update the marker) or a real regression.

Nothing here parses JavaScript. It is deliberately the same substring
matching the tests always did, with the failure mode fixed.
"""

import ast
import os

STATIC_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static")

# The frontend JS, one file per concern, in the order static/index.html
# loads them (task-89). Kept here rather than in test_server.py so the
# behaviour tier can load the same list.
FRONTEND_FILES = [
    "state.js",
    "dom.js",
    "api.js",
    "tasks.js",
    "feedback.js",
    "board.js",
    "spawn.js",
    "harvest.js",
    "sessions.js",
    "drawer.js",
    "pane.js",
    "shell.js",
    "settings.js",
    "main.js",
]


class SourceLookupError(AssertionError):
    """A marker a source-text test greps for is not in the file.

    An AssertionError on purpose: unittest reports it as a FAILURE of
    the test that read the slice, not as an error out of a fixture.
    """


def _quote(needle):
    text = needle if len(needle) <= 90 else needle[:87] + "..."
    return repr(text)


class Source(str):
    """The text of one file under `static/`, carrying its own name.

    A real `str`, so every existing whole-file assertion (`assertIn`,
    `count`, `startswith`, `re.findall`) keeps working unchanged. Only
    `index`/`rindex` differ: they raise `SourceLookupError` naming the
    file instead of a bare `ValueError`.
    """

    def __new__(cls, text, name):
        obj = super().__new__(cls, text)
        obj.name = name
        return obj

    def index(self, sub, *args):
        try:
            return str.index(self, sub, *args)
        except ValueError:
            raise SourceLookupError(
                "static/%s: cannot find %s." % (self.name, _quote(sub))
            ) from None

    def rindex(self, sub, *args):
        try:
            return str.rindex(self, sub, *args)
        except ValueError:
            raise SourceLookupError(
                "static/%s: cannot find %s (searching backwards)."
                % (self.name, _quote(sub))
            ) from None


def load_static(name):
    """Read one file from static/ as a `Source`."""
    with open(os.path.join(STATIC_DIR, name), "r", encoding="utf-8") as f:
        return Source(f.read(), name)


class SourceRegion:
    """A slice of a source file, resolved on first read rather than in setUp.

    Behaves like the `str` the tests used to hold -- `in`, `index`,
    `count`, `splitlines`, slicing, `len` -- but nothing is looked up
    until a test touches it, and a marker that moved fails the test that
    depends on it with a message naming the file, the marker and the
    invariant.
    """

    def __init__(self, source, resolve, invariant):
        self._source = source
        self._resolve = resolve
        self._invariant = invariant
        self._text = None

    # -- resolution --

    @property
    def text(self):
        if self._text is None:
            self._text = self._resolve(self._source)
        return self._text

    def fail(self, detail):
        raise SourceLookupError(
            "static/%s: %s\nThe source-text invariant %r is asserted against "
            "the spelling of the source, so this is either a rename (update "
            "the marker) or a real regression."
            % (self._source.name, detail, self._invariant))

    def locate(self, needle, start=0):
        """`index`, spelled as what it is: where in this region the marker sits."""
        at = self.text.find(needle, start)
        if at == -1:
            self.fail("cannot find %s." % _quote(needle))
        return at

    # -- the str surface the contract tests use --

    def index(self, sub, *args):
        return self.locate(sub, args[0] if args else 0)

    def rindex(self, sub, *args):
        at = self.text.rfind(sub, *args)
        if at == -1:
            self.fail("cannot find %s (searching backwards)." % _quote(sub))
        return at

    def __contains__(self, needle):
        return needle in self.text

    def __len__(self):
        return len(self.text)

    def __getitem__(self, item):
        return self.text[item]

    def __iter__(self):
        return iter(self.text)

    def __str__(self):
        return self.text

    def __add__(self, other):
        return self.text + other

    def __radd__(self, other):
        return other + self.text

    def __eq__(self, other):
        return self.text == other

    def __ne__(self, other):
        return self.text != other

    def __hash__(self):
        return hash(self.text)

    def __repr__(self):
        return "<SourceRegion %s of static/%s>" % (self._invariant, self._source.name)

    def __getattr__(self, name):
        # count, splitlines, split, replace, startswith, endswith, strip...
        # Private names are this object's own; forwarding them would
        # recurse through `text` on a half-built instance.
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(self.text, name)


def _find(source, needle, start=0, invariant=""):
    at = source.find(needle, start)
    if at == -1:
        raise SourceLookupError(
            "static/%s: cannot find %s.\nThe source-text invariant %r is "
            "asserted against the spelling of the source, so this is either "
            "a rename (update the marker) or a real regression."
            % (source.name, _quote(needle), invariant))
    return at


def region(source, start, end=None, *, invariant, trim_back_to=None):
    """The text from `start` to `end` (exclusive), resolved on first read.

    `end` is searched for AFTER `start`, so a marker that also appears
    earlier in the file is not a trap. With no `end`, the region runs to
    the end of the file. `trim_back_to` cuts the region back to the last
    occurrence of that separator before `end` -- for a section whose end
    marker belongs to what follows it.
    """

    def resolve(src):
        a = _find(src, start, 0, invariant)
        if end is None:
            return src[a:]
        b = _find(src, end, a, invariant)
        if trim_back_to is not None:
            b = src.rindex(trim_back_to, a, b)
        return src[a:b]

    return SourceRegion(source, resolve, invariant)


def function_body(source, name, *, invariant=None):
    """The source of one top-level function of a frontend file.

    From `function <name>(` to its closing brace, i.e. the first `}` back
    at the file's own top-level indentation -- independent of what
    follows it, so it works for the last function in a file too
    (task-89).
    """
    invariant = invariant or ("function %s()" % name)
    opener = "function %s(" % name

    def resolve(src):
        a = _find(src, opener, 0, invariant)
        return src[a:_find(src, "\n  }\n", a, invariant) + 5]

    return SourceRegion(source, resolve, invariant)


def to_end_of_code(source, start, *, invariant):
    """From `start` to the end of a frontend file's real code.

    Its seam block if it has one, else the closing wrapper. For the last
    function in a file, where there is no following top-level
    `function ` to stop at (task-89).
    """
    markers = ("\n  // Seam: what the other files reach for",
               "\n})(window.Centrale = window.Centrale || {});")

    def resolve(src):
        a = _find(src, start, 0, invariant)
        for marker in markers:
            idx = src.find(marker, a)
            if idx != -1:
                return src[a:src.rindex("\n", a, idx)]
        return src[a:]

    return SourceRegion(source, resolve, invariant)


# ---------------------------------------------------------------------
# The census behind task-108's AC #4
# ---------------------------------------------------------------------

#: Helpers whose presence in a test (or its fixture) means that test
#: reads frontend SOURCE rather than driving behaviour.
SOURCE_READERS = frozenset({
    "load_static", "region", "function_body", "to_end_of_code",
    # The eager helpers task-108 replaced, so the census reads the same
    # way against a pre-task-108 revision.
    "js_function", "js_to_end_of_code",
})

#: How a test that drives the real sources under node identifies itself:
#: through the shared harness, or -- before task-108 extracted it -- by
#: its own `skipUnless(shutil.which("node"), ...)` decorator.
BEHAVIOUR_MARKERS = frozenset({"run_driver", "requires_node"})


def _names_used(node):
    used = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Name):
            used.add(child.id)
        elif isinstance(child, ast.Attribute):
            used.add(child.attr)
    return used


def census(path):
    """Classify every test in one test module by what it reads.

    Returns `{"total", "source_text", "behaviour", "classes"}`, where
    `source_text` counts tests that assert on frontend source text and
    `behaviour` counts tests that drive the real sources under node. A
    test whose class fixture reads source but which itself only drives
    the harness counts as behavioural: the fixture is incidental.
    """
    with open(path, "r", encoding="utf-8") as f:
        tree = ast.parse(f.read())
    total = source_text = behaviour = 0
    classes = {}
    for cls in tree.body:
        if not isinstance(cls, ast.ClassDef):
            continue
        fixtures = [f for f in cls.body
                    if isinstance(f, ast.FunctionDef) and f.name in ("setUp", "setUpClass")]
        fixture_reads = any(_names_used(f) & SOURCE_READERS for f in fixtures)
        # A whole class can declare itself behavioural, by requiring node
        # or by reaching for the harness in a shared helper.
        cls_decorated = " ".join(ast.unparse(d) for d in cls.decorator_list)
        cls_behaviour = not fixture_reads and ("node" in cls_decorated or bool(
            {n for f in cls.body if not (isinstance(f, ast.FunctionDef)
                                         and f.name.startswith("test"))
             for n in _names_used(f)} & BEHAVIOUR_MARKERS))
        for fn in cls.body:
            if not isinstance(fn, ast.FunctionDef) or not fn.name.startswith("test"):
                continue
            total += 1
            used = _names_used(fn)
            decorated = " ".join(ast.unparse(d) for d in fn.decorator_list)
            if (used & BEHAVIOUR_MARKERS) or "node" in decorated or (
                    cls_behaviour and not (used & SOURCE_READERS)):
                behaviour += 1
            elif fixture_reads or (used & SOURCE_READERS):
                source_text += 1
                classes[cls.name] = classes.get(cls.name, 0) + 1
    return {"total": total, "source_text": source_text,
            "behaviour": behaviour, "classes": classes}
