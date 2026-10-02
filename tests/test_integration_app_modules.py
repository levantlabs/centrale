"""Guard for tests_integration/base.APP_MODULES (TASK-198).

The integration tier deploys a copy of the app from the hand-kept
APP_MODULES tuple. A top-level module the app imports but the tuple omits
makes every real-server integration test die at import -- and that tier
only runs in the release gate, so the gap used to surface at release time
(version.py via task-107, fleet.py via TASK-186). This walks server.py's
local imports, transitively, and fails here, in the fast suite, instead.

APP_MODULES stays an explicit list on purpose; this test checks it rather
than replacing it with a glob. Scripts and test helpers are never imported
by the app, so they are never required.
"""

import ast
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from tests_integration import base  # noqa: E402

ENTRY = "server.py"


def top_level_modules(root=ROOT):
    return {f for f in os.listdir(root)
            if f.endswith(".py") and os.path.isfile(os.path.join(root, f))}


def local_imports(filename, available):
    """Top-level repo modules imported by `filename`, including imports
    nested in functions or try blocks (ast.walk visits them all)."""
    with open(os.path.join(ROOT, filename), encoding="utf-8") as f:
        tree = ast.parse(f.read(), filename)
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names = [node.module]
            # `from pkg import mod` where mod is itself a top-level module
            names += [alias.name for alias in node.names]
        else:
            continue
        for name in names:
            candidate = name.split(".")[0] + ".py"
            if candidate in available:
                found.add(candidate)
    return found


def app_closure(entry=ENTRY, root=ROOT):
    available = top_level_modules(root)
    seen, todo = set(), [entry]
    while todo:
        current = todo.pop()
        if current in seen:
            continue
        seen.add(current)
        todo.extend(local_imports(current, available) - seen)
    return seen


class AppModulesCoverTheImportClosureTest(unittest.TestCase):
    def test_every_imported_top_level_module_is_in_app_modules(self):
        missing = sorted(app_closure() - set(base.APP_MODULES))
        self.assertEqual(
            missing, [],
            f"imported by {ENTRY} (transitively) but missing from "
            f"tests_integration/base.py APP_MODULES: {', '.join(missing)}")

    def test_closure_walk_sees_known_modules(self):
        # Guards the walker itself: if it silently found nothing, the test
        # above would pass vacuously.
        closure = app_closure()
        for name in ("server.py", "fleet.py", "version.py", "orchestrator.py"):
            self.assertIn(name, closure)

    def test_app_modules_all_exist(self):
        for name in base.APP_MODULES:
            self.assertTrue(os.path.isfile(os.path.join(ROOT, name)), name)


if __name__ == "__main__":
    unittest.main()
