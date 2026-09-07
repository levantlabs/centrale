"""Run the real `static/*.js` sources under node, over a minimal DOM shim.

Centrale ships no build step and no browser test dependency, so the
frontend's BEHAVIOUR is exercised the way task-94 first did it: node
evaluates the actual source files over a hand-written DOM/window shim
small enough to read in one sitting, and a driver script walks the flow
under test and prints JSON for the Python assertions.

That is deliberately not a browser emulation. The shim implements only
what `static/dom.js`'s `h()`/`clearChildren()`/`byId()` and the render
paths actually touch -- element trees, classes, attributes, listeners,
a localStorage, and enough of the scroll/input surface for the live
pane -- so what a test exercises is Centrale's JavaScript rather than a
reimplementation of the platform.

Before task-108 this shim was a string literal inside
`tests/test_server.py`, copied into each task's own driver. It lives here
now so every behavioural test shares one shim, and so
`tests/test_frontend_behaviour.py` can add flows without growing another
copy.

`node` is optional in development: every test that uses this module is
decorated with `@requires_node` and skips cleanly on a machine without
it, the same way `tests_integration/base.require_tools` handles a missing
`git`/`tmux`. `python3 -m unittest discover tests` on a machine with no
node therefore still reports OK, with this whole tier counted as skips.

A RELEASE is the one place that is not good enough (task-124). The
release gate runs the suite inside the staged snapshot, and a skipped
behavioural tier there means nothing executed the frontend at all -- the
source-shape contracts grep text, they never parse or run it, so a
snapshot whose JavaScript cannot even be parsed passed the gate. So
`scripts/release.sh` requires `node` on the release machine AND exports
`CENTRALE_REQUIRE_NODE=1` for that run: with it set, an absent `node` is
a FAILURE here rather than a skip, and the release aborts with nothing
pushed.
"""

import functools
import json
import os
import shutil
import subprocess
import unittest

STATIC_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static")


def node_path():
    return shutil.which("node")


#: Set by `scripts/release.sh` around the staged snapshot's suite run
#: (see the module docstring): a missing runtime for this tier stops
#: being an acceptable skip and becomes a failure. Not set anywhere in
#: ordinary development, where the skip is the intended behaviour.
REQUIRE_NODE_ENV = "CENTRALE_REQUIRE_NODE"

NODE_REQUIRED_MESSAGE = (
    "node is not on PATH and %s is set, so this test cannot be skipped: "
    "something asked for the frontend behavioural tier to actually RUN "
    "(a release gate does -- a snapshot whose JavaScript was never "
    "executed is exactly what it exists to catch). Install node on this "
    "machine, or unset %s if a skip really is acceptable here."
    % (REQUIRE_NODE_ENV, REQUIRE_NODE_ENV))


def node_required():
    """True when a caller has declared a skip of this tier unacceptable."""
    return os.environ.get(REQUIRE_NODE_ENV, "").strip() not in ("", "0")


def _fails_without_node(fn):
    @functools.wraps(fn)
    def stub(self, *args, **kwargs):
        raise AssertionError(NODE_REQUIRED_MESSAGE)
    return stub


def requires_node(obj):
    """Skip when this machine has no `node` -- unless a caller set
    `CENTRALE_REQUIRE_NODE`, when the same absence is a failure instead
    (see the module docstring).

    Decorates a test method or a whole TestCase class, because both
    spellings are in use; the class form replaces its test methods
    rather than marking the class skipped, so the failure arrives once
    per test with a message that says what is missing.
    """
    if node_path():
        return obj
    if not node_required():
        return unittest.skip("node not available")(obj)
    if isinstance(obj, type):
        for name, value in list(vars(obj).items()):
            if name.startswith("test") and callable(value):
                setattr(obj, name, _fails_without_node(value))
        return obj
    return _fails_without_node(obj)


# ---------------------------------------------------------------------
# The shim
# ---------------------------------------------------------------------

SHIM_JS = r"""
function El(tag) {
  this.tagName = tag;
  this.childNodes = [];
  this.attrs = {};
  this.style = {};
  this.className = "";
  this.title = "";
  this.disabled = false;
  this.hidden = false;
  this.parentNode = null;
  this._text = "";
  this.listeners = {};
  // The scroll surface pane.js reads. Plain numbers: a test that cares
  // sets them, and a test that does not gets a document that fits.
  this.scrollTop = 0;
  this.scrollHeight = 0;
  this.clientHeight = 0;
  this.focused = false;
}
Object.defineProperty(El.prototype, "textContent", {
  get: function () {
    return this._text + this.childNodes.map(function (c) { return c.textContent; }).join("");
  },
  set: function (v) { this.childNodes = []; this._text = String(v); }
});
Object.defineProperty(El.prototype, "firstChild", {
  get: function () { return this.childNodes[0] || null; }
});
El.prototype._adopt = function (c) {
  if (c && c.parentNode && c.parentNode !== this) c.parentNode.removeChild(c);
  if (c) c.parentNode = this;
  return c;
};
El.prototype.appendChild = function (c) {
  this._adopt(c);
  this.childNodes.push(c);
  return c;
};
El.prototype.insertBefore = function (c, ref) {
  this._adopt(c);
  var i = ref ? this.childNodes.indexOf(ref) : -1;
  if (i === -1) this.childNodes.push(c); else this.childNodes.splice(i, 0, c);
  return c;
};
El.prototype.removeChild = function (c) {
  var i = this.childNodes.indexOf(c);
  if (i !== -1) this.childNodes.splice(i, 1);
  if (c) c.parentNode = null;
  return c;
};
El.prototype.setAttribute = function (k, v) {
  this.attrs[k] = String(v);
  if (k === "id") this.id = String(v); // so getElementById can find a built node
};
El.prototype.getAttribute = function (k) {
  return Object.prototype.hasOwnProperty.call(this.attrs, k) ? this.attrs[k] : null;
};
// task-97: the drawer's section headers fold by toggling a class, so the
// shim grew the classList surface the real DOM has. Additive --
// className stays the single source of truth underneath it.
Object.defineProperty(El.prototype, "classList", {
  get: function () {
    var el = this;
    function list() { return String(el.className || "").split(/\s+/).filter(Boolean); }
    function write(arr) { el.className = arr.join(" "); }
    return {
      contains: function (c) { return list().indexOf(c) !== -1; },
      add: function (c) { var a = list(); if (a.indexOf(c) === -1) { a.push(c); write(a); } },
      remove: function (c) { write(list().filter(function (x) { return x !== c; })); },
      toggle: function (c, force) {
        var has = list().indexOf(c) !== -1;
        var want = force === undefined ? !has : !!force;
        if (want && !has) { var a = list(); a.push(c); write(a); }
        if (!want && has) { write(list().filter(function (x) { return x !== c; })); }
        return want;
      }
    };
  }
});
// Called as querySelectorAll("input, button") -- a tag-name list -- as
// querySelectorAll(".column-body[data-status]"), which is how
// renderBoard finds the lanes whose scroll offsets it carries across a
// re-render (task-50), and (task-158) as the settings modal's
// '#settings-check-commands [data-project-error="my-app"]'. So: a
// comma-separated list of selectors, each one or more space-separated
// compounds (the descendant combinator), each compound a tag name
// and/or "#id" and/or ".class" and/or "[attr]" / '[attr="value"]'
// parts. Anything more than that throws by name rather than silently
// matching nothing -- a selector this cannot parse is a test reading an
// empty list and passing.
function parseCompound(text) {
  var m = String(text).match(
    /^([a-zA-Z][\w-]*)?((?:[#.][\w-]+|\[[\w-]+(?:="[^"]*")?\])*)$/);
  if (!m || (!m[1] && !m[2])) {
    throw new Error("the DOM shim cannot parse the selector " + JSON.stringify(text));
  }
  var ids = (m[2].match(/#[\w-]+/g) || []).map(function (c) { return c.slice(1); });
  return {
    tag: m[1] ? m[1].toLowerCase() : null,
    id: ids.length ? ids[ids.length - 1] : null,
    classes: (m[2].match(/\.[\w-]+/g) || []).map(function (c) { return c.slice(1); }),
    attrs: (m[2].match(/\[[\w-]+(?:="[^"]*")?\]/g) || []).map(function (a) {
      var body = a.slice(1, -1);
      var eq = body.indexOf("=");
      return eq === -1
        ? { name: body, value: null }
        : { name: body.slice(0, eq), value: body.slice(eq + 2, -1) };
    })
  };
}
function parseSelectorList(selector) {
  return String(selector).split(",").map(function (one) {
    var compounds = one.trim().split(/\s+/).filter(Boolean).map(parseCompound);
    if (!compounds.length) {
      throw new Error("the DOM shim cannot parse the selector " + JSON.stringify(selector));
    }
    return compounds;
  });
}
function matchesCompound(el, c) {
  if (c.tag && String(el.tagName).toLowerCase() !== c.tag) return false;
  if (c.id && el.id !== c.id) return false;
  var classes = String(el.className || "").split(/\s+/);
  if (!c.classes.every(function (x) { return classes.indexOf(x) !== -1; })) return false;
  return c.attrs.every(function (a) {
    var v = el.getAttribute(a.name);
    return a.value === null ? v !== null : v === a.value;
  });
}
// A descendant chain matches right-to-left, and -- like the real DOM --
// the ancestors it walks up through are not limited to the subtree the
// query was rooted at.
function matchesSelector(el, alternatives) {
  return alternatives.some(function (compounds) {
    if (!matchesCompound(el, compounds[compounds.length - 1])) return false;
    var i = compounds.length - 2;
    var node = el.parentNode;
    while (i >= 0) {
      if (!node) return false;
      if (matchesCompound(node, compounds[i])) i--;
      node = node.parentNode;
    }
    return true;
  });
}
function queryDescendants(node, selector, out) {
  var alternatives = parseSelectorList(selector);
  (function walk(n) {
    n.childNodes.forEach(function (c) {
      if (matchesSelector(c, alternatives)) out.push(c);
      walk(c);
    });
  })(node);
  return out;
}
El.prototype.querySelectorAll = function (selector) {
  return queryDescendants(this, selector, []);
};
El.prototype.querySelector = function (selector) {
  return this.querySelectorAll(selector)[0] || null;
};
El.prototype.addEventListener = function (type, fn) {
  (this.listeners[type] = this.listeners[type] || []).push(fn);
};
// task-127: focus is now a property of the DOCUMENT, not only a flag on
// the element -- the drawer's own code reads document.activeElement to
// find out what the viewer had focused before a rebuild, so a shim
// where focus() only set a local boolean could not exercise it. The
// per-element flag stays: task-126's probe reads it.
El.prototype.focus = function () {
  if (document.activeElement && document.activeElement !== this) {
    document.activeElement.focused = false;
  }
  this.focused = true;
  document.activeElement = this;
};
El.prototype.blur = function () {
  this.focused = false;
  if (document.activeElement === this) document.activeElement = null;
};
function makeEvent(props) {
  var ev = { stopPropagation: function () {}, preventDefault: function () {} };
  Object.keys(props || {}).forEach(function (k) { ev[k] = props[k]; });
  return ev;
}
El.prototype.dispatch = function (type, props) {
  var ev = makeEvent(props);
  (this.listeners[type] || []).forEach(function (fn) { fn(ev); });
  return ev;
};
El.prototype.click = function () { return this.dispatch("click", {}); };
El.prototype.key = function (key) { return this.dispatch("keydown", { key: key }); };

// The ids static/index.html actually declares. Those -- and only those
// -- are stood up on demand as the document shell; every other id
// resolves against the tree the sources have built, and comes back null
// when nothing has built it. Getting that right is load bearing: the
// live pane's own elements are created by h() and then found again by
// C.byId, and half of pane.js's decisions ("is there a reply row?", "is
// there a pane to maximize?") are exactly that null check.
var SHELL_IDS = {};
try {
  var __html = require("fs").readFileSync(
    require("path").join(process.argv[1], "index.html"), "utf8");
  (__html.match(/id="[^"]+"/g) || []).forEach(function (m) { SHELL_IDS[m.slice(4, -1)] = true; });
} catch (e) { /* a driver that passes no static dir gets the tree search alone */ }

var ELEMENTS = {};
var DOC_LISTENERS = {};
function findById(node, id) {
  for (var i = 0; i < node.childNodes.length; i++) {
    var c = node.childNodes[i];
    if (c.id === id) return c;
    var hit = findById(c, id);
    if (hit) return hit;
  }
  return null;
}
global.document = {
  // Nothing focused until something is: a real document would say
  // <body> here, and the drawer treats "not inside the drawer body"
  // and "nothing at all" the same way, so null is the honest stand-in.
  activeElement: null,
  createElement: function (tag) { return new El(tag); },
  createTextNode: function (text) { var n = new El("#text"); n.textContent = text; return n; },
  getElementById: function (id) {
    if (ELEMENTS[id] && !ELEMENTS[id].parentNode) return ELEMENTS[id];
    var keys = Object.keys(ELEMENTS);
    for (var i = 0; i < keys.length; i++) {
      if (ELEMENTS[keys[i]].id === id) return ELEMENTS[keys[i]];
      var hit = findById(ELEMENTS[keys[i]], id);
      if (hit) return hit;
    }
    if (!SHELL_IDS[id] && Object.keys(SHELL_IDS).length) return null;
    ELEMENTS[id] = new El("div");
    ELEMENTS[id].id = id;
    return ELEMENTS[id];
  },
  addEventListener: function (type, fn) {
    (DOC_LISTENERS[type] = DOC_LISTENERS[type] || []).push(fn);
  },
  // task-158: settings.js queries from the DOCUMENT, not from a node it
  // is already holding ('#settings-modal .settings-field-error'), so the
  // shim needs the same two methods there. "The document" here is every
  // root the sources have built or the shell has stood up.
  querySelectorAll: function (selector) {
    var out = [];
    documentRoots().forEach(function (root) {
      if (matchesSelector(root, parseSelectorList(selector))) out.push(root);
      queryDescendants(root, selector, out);
    });
    return out;
  },
  querySelector: function (selector) {
    return document.querySelectorAll(selector)[0] || null;
  }
};
// Every top-most node reachable from the ids handed out so far, in the
// order those ids were first asked for.
function documentRoots() {
  var roots = [];
  Object.keys(ELEMENTS).forEach(function (k) {
    var top = ELEMENTS[k];
    while (top.parentNode) top = top.parentNode;
    if (roots.indexOf(top) === -1) roots.push(top);
  });
  return roots;
}
// Press one key on the document -- the Escape ladder's only entry point.
global.pressKey = function (key) {
  var ev = makeEvent({ key: key });
  (DOC_LISTENERS.keydown || []).forEach(function (fn) { fn(ev); });
};
var STORE = {};
var WIN_LISTENERS = {};
global.window = {
  innerWidth: 1440,
  innerHeight: 900,
  localStorage: {
    getItem: function (k) { return Object.prototype.hasOwnProperty.call(STORE, k) ? STORE[k] : null; },
    setItem: function (k, v) { STORE[k] = String(v); },
    removeItem: function (k) { delete STORE[k]; }
  },
  addEventListener: function (type, fn) {
    (WIN_LISTENERS[type] = WIN_LISTENERS[type] || []).push(fn);
  },
  matchMedia: function () { return { matches: false, addEventListener: function () {} }; }
};
// How many elements carrying this id are reachable at all -- the
// question "did the theater MOVE the pane node, or copy it?" asked
// directly, rather than by grepping for cloneNode.
global.countById = function (id) {
  var seen = [];
  function walk(node) {
    if (node.id === id && seen.indexOf(node) === -1) seen.push(node);
    node.childNodes.forEach(walk);
  }
  Object.keys(ELEMENTS).forEach(function (k) { walk(ELEMENTS[k]); });
  return seen.length;
};

global.fireWindow = function (type) {
  (WIN_LISTENERS[type] || []).forEach(function (fn) { fn(makeEvent({ type: type })); });
};

// The parent/child relationships static/index.html declares, for the
// flows that re-parent a node between them (task-73's theater moves
// #drawer-pane-area into #theater and back). Opt-in: a driver that only
// renders into one area needs none of it.
global.wireDrawerShell = function () {
  var drawer = document.getElementById("drawer");
  ["drawer-body", "drawer-session-area", "drawer-pane-area",
   "drawer-harvest-area", "drawer-spawn-area"].forEach(function (id) {
    drawer.appendChild(document.getElementById(id));
  });
  return drawer;
};

// The same, for the settings modal (task-158). Opt-in for the same
// reason: only a driver that queries ACROSS the modal needs it --
// clearSettingsFieldErrors() asks the document for
// "#settings-modal .settings-field-error", which finds nothing unless
// those nodes really hang off #settings-modal. Flat rather than nested:
// every one of these is a descendant of the modal in index.html, and a
// descendant combinator is all the sources ask about.
global.wireSettingsShell = function () {
  var modal = document.getElementById("settings-modal");
  ["settings-header", "settings-title", "settings-close", "settings-body",
   "settings-harvest-mode-toggle", "settings-error-harvestMode",
   "settings-session-preview-toggle", "settings-session-reply-field",
   "settings-session-reply-toggle", "settings-error-sessionPreviewMode",
   "settings-refresh-interval", "settings-error-refreshIntervalSeconds",
   "settings-check-commands",
   "settings-agents-section", "settings-agents-summary", "settings-agents-table",
   "settings-add-agent-btn", "settings-error-agents", "settings-default-agent",
   "settings-error-defaultAgent",
   "settings-projects-list",
   "settings-add-project-name", "settings-error-addProject.name",
   "settings-add-project-path", "settings-error-addProject.path",
   "settings-add-project-init", "settings-add-project-status",
   "settings-add-project-btn",
   "settings-footer", "settings-status", "settings-save"].forEach(function (id) {
    var el = document.getElementById(id);
    // index.html declares every "settings-error-*" node as a hidden
    // .settings-field-error, and both halves are load bearing: that is
    // the class clearSettingsFieldErrors() sweeps by, and a shell node
    // the shim stood up bare would silently never be found or cleared.
    if (id.indexOf("settings-error-") === 0) {
      el.className = "settings-field-error";
      el.hidden = true;
    }
    modal.appendChild(el);
  });
  return modal;
};
"""


# ---------------------------------------------------------------------
# Running a driver
# ---------------------------------------------------------------------

def run_driver(case, driver_js, *args, timeout=60):
    """Run `driver_js` over the shim and return the JSON it printed.

    `process.argv[1]` is `static/`; any further `args` follow it. A
    non-zero exit fails the calling test with node's stderr, so a
    driver that throws says where.

    Reached without a `node` at all only when `requires_node` was told
    not to skip (or was never applied), so say that in words rather
    than dying inside `subprocess.run` on a None executable.
    """
    if not node_path():
        raise AssertionError(NODE_REQUIRED_MESSAGE)
    proc = subprocess.run(
        [node_path(), "-e", SHIM_JS + driver_js, STATIC_DIR] + [str(a) for a in args],
        capture_output=True, text=True, timeout=timeout)
    case.assertEqual(proc.returncode, 0,
                     "the node driver failed:\n" + proc.stderr)
    try:
        return json.loads(proc.stdout)
    except ValueError:
        case.fail("the node driver printed no JSON:\n" + proc.stdout + proc.stderr)


_RESULTS = {}


def cached_driver(case, driver_js, *args, timeout=60):
    """`run_driver`, run once per process and shared by a class of tests.

    Deliberately not a `setUpClass`: task-108's whole point is that a
    fixture is the wrong place to do work that can fail. Running the
    driver on first read means a driver that throws FAILS the test that
    needed it, with node's stderr, instead of erroring every test in the
    class with a stack trace from unittest's plumbing.
    """
    key = (driver_js, args)
    if key not in _RESULTS:
        _RESULTS[key] = run_driver(case, driver_js, *args, timeout=timeout)
    return _RESULTS[key]


#: Prelude every driver starts with: load the real sources, in
#: index.html's order, over the shim above.
LOAD_SOURCES_JS = r"""
var fs = require("fs");
var path = require("path");
var vm = require("vm");
var STATIC = process.argv[1];
function loadFrontend(names) {
  names.forEach(function (name) {
    vm.runInThisContext(fs.readFileSync(path.join(STATIC, name), "utf8"), { filename: name });
  });
  return window.Centrale;
}
// Held before a driver replaces global.setTimeout with a controllable
// one: `tick` must always drain the microtask queue for real.
var REAL_SET_TIMEOUT = setTimeout;
function tick() { return new Promise(function (r) { REAL_SET_TIMEOUT(r, 0); }); }
function settle() { return tick().then(tick).then(tick); }
"""
