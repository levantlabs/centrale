#!/usr/bin/env python3
"""Regenerate docs/img/*.png from committed synthetic data (task-153).

The four images the README and docs lead with -- board-light.png,
board-dark.png, drawer-pane.png, session-theater.png -- were hand-taken
until now, so they drifted silently as the UI moved and nobody could
tell how stale they were. This script re-shoots all four the same way
every time.

WHAT IT WILL NOT DO
-------------------
It never renders the maintainer's real board, and it has no flag that
would let it. That is not a convention here, it is the whole point: a
screenshot is the one published artifact scripts/scan_release.py cannot
check, because that scan reads text and an image is pixels. So:

  * projects.json is never read. The config is built in memory from
    scripts/screenshots_fixture.json, over throwaway directories in a
    fresh temp sandbox.
  * every subprocess boundary the server uses (run_backlog,
    run_backlog_raw, run_git, run_tmux, which) is replaced by a fake
    that answers only from the fixture AND REFUSES any working
    directory outside that sandbox. A refusal is recorded and makes the
    whole run exit non-zero, so it can never pass unnoticed.
  * no real agent is started. The "live" session in the drawer and
    theater shots is a fixture pane fed through the same faked tmux
    boundary; nothing is spawned, no tmux server is contacted.

Everything else is real: this drives the actual server.py request
handlers and the actual static/ frontend in a real browser, so what the
images show is what the code renders.

DEPENDENCIES -- MAINTAINER ONLY
-------------------------------
Playwright and Pillow are needed to run this and are needed nowhere
else. Centrale itself is Python-3.12-stdlib-only with no build step,
and the release gate already asks the release machine for node and tmux;
a documentation image is not worth a third release prerequisite. So
nothing invokes this script -- no test, no scripts/release.sh, no
runtime path -- and when either package is missing it says so and exits
instead of failing anything.

    python3 -m pip install playwright pillow
    python3 -m playwright install chromium

RESOURCES OUTSIDE THE REPO
--------------------------
  * a temp sandbox directory (TMPDIR), removed on exit unless --keep;
  * one ephemeral 127.0.0.1 port, chosen by the OS (bind to 0);
  * XDG_CACHE_HOME, redirected into the sandbox for the run so nothing
    can touch a real ~/.cache/centrale;
  * CENTRALE_SCREENSHOT_CHROMIUM, optional: an explicit browser
    executable for Playwright to launch instead of its own download.

See docs/operations.md, "Regenerating the documentation screenshots".
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(SCRIPT_DIR)
FIXTURE_PATH = os.path.join(SCRIPT_DIR, "screenshots_fixture.json")
DEFAULT_OUT_DIR = os.path.join(REPO_ROOT, "docs", "img")

# The size every committed image already has: a 1440x900 viewport shot at
# device_scale_factor 2. Changing either number reshapes all four files,
# so they live here as one pair rather than at each call site.
VIEWPORT = {"width": 1440, "height": 900}
DEVICE_SCALE_FACTOR = 2

# The app's own theme key (see the bootstrap script at the top of
# static/index.html): setting it before any script runs is how a shot
# picks light or dark explicitly, rather than inheriting whatever the
# machine's prefers-color-scheme happens to say.
THEME_STORAGE_KEY = "centrale-theme"


class Refusal(RuntimeError):
    """A faked boundary was asked for a directory outside the sandbox."""


def _fatal(message, code=2):
    print(message, file=sys.stderr)
    raise SystemExit(code)


def require_playwright():
    try:
        from playwright.sync_api import sync_playwright  # noqa: F401
    except ImportError:
        _fatal(
            "playwright is not installed.\n"
            "\n"
            "It is a maintainer-only tool: nothing in Centrale's tests, release\n"
            "gate or runtime uses it, and this script is the only thing that\n"
            "wants it. Install it just to regenerate the documentation images:\n"
            "\n"
            "    python3 -m pip install playwright pillow\n"
            "    python3 -m playwright install chromium\n"
            "\n"
            "Nothing else is affected by its absence."
        )
    from playwright.sync_api import sync_playwright
    return sync_playwright


def require_pillow():
    try:
        from PIL import Image
    except ImportError:
        _fatal(
            "pillow is not installed.\n"
            "\n"
            "It quantizes the shots to the 256-colour palette the committed\n"
            "images already use, which is what keeps them a drop-in replacement\n"
            "rather than files three times the size. Like playwright it is a\n"
            "maintainer-only tool:\n"
            "\n"
            "    python3 -m pip install playwright pillow\n"
            "\n"
            "Nothing else is affected by its absence."
        )
    return Image


# ---------------------------------------------------------------------------
# The synthetic world
# ---------------------------------------------------------------------------

def _proc(args, returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(args, returncode, stdout, stderr)


def task_id_from_branch(branch):
    return branch.rsplit("/", 1)[-1].upper()


class World:
    """The fixture, stood up as directories plus faked CLI boundaries.

    Two rules hold everything else together:

      * every path this hands out is under `root`, a fresh temp dir;
      * every boundary call whose cwd is not under `root` raises Refusal
        and is remembered, so a run that touched anything real ends in a
        non-zero exit naming what it touched.
    """

    def __init__(self, fixture, root):
        self.fixture = fixture
        self.root = os.path.realpath(root)
        self.repos_root = os.path.join(self.root, "repos")
        self.worktree_root = os.path.join(self.root, "worktrees")
        self.projects = fixture["projects"]
        self.by_name = {p["name"]: p for p in self.projects}
        self.refusals = []
        self.unhandled = []

    # -- layout ---------------------------------------------------------

    def repo_path(self, name):
        return os.path.join(self.repos_root, name)

    def worktree_path(self, name, task_id):
        # Must agree with spawn.worktree_dir(config, name, task_id) given
        # config["worktreeRoot"] == self.worktree_root.
        return os.path.join(self.worktree_root, f"{name}-{task_id.lower()}")

    def build(self):
        """Create the directories the server stats: a directory per
        project (`path` must exist or the project reads as an error) and
        one per task branch (an existing Centrale worktree is what makes
        the drawer read the branch's copy of a task, and what makes the
        sessions panel list touched files)."""
        for project in self.projects:
            os.makedirs(self.repo_path(project["name"]), exist_ok=True)
            for branch in project.get("taskBranches", []):
                os.makedirs(
                    self.worktree_path(project["name"], task_id_from_branch(branch)),
                    exist_ok=True,
                )

    def config(self):
        return {
            "port": 0,
            "worktreeRoot": self.worktree_root,
            "projects": [
                {"name": p["name"], "path": self.repo_path(p["name"])}
                for p in self.projects
            ],
        }

    # -- the sandbox guard ----------------------------------------------

    def _inside(self, path):
        if not path:
            return False
        real = os.path.realpath(path)
        return real == self.root or real.startswith(self.root + os.sep)

    def _guard(self, tool, args, cwd):
        if self._inside(cwd):
            return
        note = f"{tool} {' '.join(args)} (cwd={cwd!r})"
        self.refusals.append(note)
        raise Refusal(
            f"refused a {tool} call outside the screenshot sandbox: {note}"
        )

    def _locate(self, cwd):
        """(project, task_id) for a sandbox cwd: task_id is None for a
        project's own checkout, set for one of its worktrees."""
        real = os.path.realpath(cwd)
        for project in self.projects:
            if real == os.path.realpath(self.repo_path(project["name"])):
                return project, None
            for branch in project.get("taskBranches", []):
                task_id = task_id_from_branch(branch)
                if real == os.path.realpath(self.worktree_path(project["name"], task_id)):
                    return project, task_id
        return None, None

    # -- backlog data ----------------------------------------------------

    def _task_stub(self, task):
        stub = {
            "id": task["id"],
            "title": task["title"],
            "status": task["status"],
            "type": task.get("type"),
            "priority": task.get("priority"),
            "assignees": task.get("assignees") or [],
            "reporter": None,
            "labels": task.get("labels") or [],
            "milestone": task.get("milestone"),
            "parentTaskId": None,
            "ordinal": task.get("ordinal"),
            "createdAt": task.get("createdAt"),
            "updatedAt": task.get("updatedAt"),
        }
        return stub

    def task_list(self, project, ready_only=False):
        tasks = [self._task_stub(t) for t in project["tasks"]]
        if ready_only:
            ready = set(project.get("ready") or [])
            tasks = [t for t in tasks if t["id"] in ready]
        return {"schemaVersion": 1, "kind": "task-list", "tasks": tasks}

    def task_view(self, project, task_id, on_branch=False):
        raw = next((t for t in project["tasks"] if t["id"] == task_id), None)
        if raw is None:
            raise KeyError(task_id)
        task = self._task_stub(raw)
        task.update({
            "path": f"backlog/tasks/{task_id.lower()} - {raw['title'].replace(' ', '-')}.md",
            "description": "",
            "dependencies": [],
            "references": [],
            "documentation": [],
            "modifiedFiles": [],
            "subtasks": [],
            "acceptanceCriteria": [],
            "implementationPlan": None,
            "implementationNotes": None,
        })
        task.update(project.get("views", {}).get(task_id) or {})
        if on_branch:
            task.update(project.get("branchViews", {}).get(task_id) or {})
        return {"schemaVersion": 1, "kind": "task-view", "task": task}

    # -- the faked boundaries --------------------------------------------

    def run_backlog(self, args, cwd):
        import server

        self._guard("backlog", args, cwd)
        project, task_id_of_cwd = self._locate(cwd)
        if project is None:
            raise server.BacklogError(f"no such backlog repo in the sandbox: {cwd}")
        if args[:2] == ["task", "list"]:
            return self.task_list(project, ready_only="--ready" in args)
        if args[:2] == ["task", "view"] and len(args) >= 3:
            try:
                return self.task_view(project, args[2], on_branch=task_id_of_cwd is not None)
            except KeyError:
                raise server.BacklogError(f"task {args[2]} not found") from None
        self.unhandled.append(f"backlog {' '.join(args)}")
        raise server.BacklogError(f"unhandled in the screenshot fixture: backlog {' '.join(args)}")

    def run_backlog_raw(self, args, cwd):
        self._guard("backlog", args, cwd)
        if args[:2] == ["milestone", "list"]:
            # No milestones in the fixture: an empty list, not a failure.
            return _proc(["backlog", *args], 0, "", "")
        self.unhandled.append(f"backlog-raw {' '.join(args)}")
        return _proc(["backlog", *args], 1, "", "unhandled in the screenshot fixture")

    def run_git(self, args, cwd=None):
        self._guard("git", args, cwd)
        project, task_id_of_cwd = self._locate(cwd)
        if project is None:
            return _proc(["git", *args], 1, "", "not a repository in the sandbox")
        branches = list(project.get("taskBranches", []))
        base = project.get("baseBranch", "main")

        if task_id_of_cwd is not None:
            # Inside a worktree: the two calls the sessions panel and the
            # board's worktreeDirty flag make.
            if args == ["status", "--porcelain"]:
                lines = (project.get("worktreeStatus") or {}).get(task_id_of_cwd) or []
                return _proc(["git", *args], 0, "".join(f"{ln}\n" for ln in lines), "")
            if args == ["diff", "--name-only", "HEAD"]:
                files = (project.get("worktreeDiff") or {}).get(task_id_of_cwd) or []
                return _proc(["git", *args], 0, "".join(f"{f}\n" for f in files), "")

        if args[:1] == ["for-each-ref"]:
            ref = args[-1]
            if ref == "refs/heads/task":
                return _proc(["git", *args], 0, "".join(f"{b}\n" for b in branches), "")
            if ref == "refs/tags/abandoned":
                # No discarded attempts in the fixture.
                return _proc(["git", *args], 0, "", "")
        if args == ["worktree", "list", "--porcelain"]:
            return _proc(["git", *args], 0, self._worktree_porcelain(project), "")
        if args == ["symbolic-ref", "--short", "HEAD"]:
            return _proc(["git", *args], 0, f"{base}\n", "")
        if args[:3] == ["rev-parse", "--verify", "--quiet"] and len(args) == 4:
            ref = args[3]
            short = ref[len("refs/heads/"):] if ref.startswith("refs/heads/") else ref
            if short in branches or short == base:
                return _proc(["git", *args], 0, f"{self._fake_sha(project, short)}\n", "")
            return _proc(["git", *args], 1, "", "")
        if args[:2] == ["merge-base", "--is-ancestor"]:
            # Nothing in the fixture has been merged into its base yet.
            return _proc(["git", *args], 1, "", "")

        self.unhandled.append(f"git {' '.join(args)} (cwd={cwd})")
        return _proc(["git", *args], 1, "", "unhandled in the screenshot fixture")

    def _worktree_porcelain(self, project):
        blocks = [
            "worktree %s\nHEAD %s\nbranch refs/heads/%s\n"
            % (
                self.repo_path(project["name"]),
                self._fake_sha(project, project.get("baseBranch", "main")),
                project.get("baseBranch", "main"),
            )
        ]
        for branch in project.get("taskBranches", []):
            task_id = task_id_from_branch(branch)
            blocks.append(
                "worktree %s\nHEAD %s\nbranch refs/heads/%s\n"
                % (
                    self.worktree_path(project["name"], task_id),
                    self._fake_sha(project, branch),
                    branch,
                )
            )
        return "\n".join(blocks)

    @staticmethod
    def _fake_sha(project, ref):
        """A stable, obviously-invented 40-hex SHA. Deterministic (it is
        derived from the names, not from a clock or a real object), and
        never shown in the UI -- git ancestry and existence checks are
        all that read it."""
        seed = f"{project['name']}:{ref}"
        digest = 0
        for ch in seed:
            digest = (digest * 131 + ord(ch)) % (16 ** 10)
        return (f"{digest:010x}" * 4)[:40]

    def run_tmux(self, args, input=None):
        if args[:1] == ["list-sessions"]:
            rows = "".join(
                "%s\t%s\t%s\n" % (s["name"], s["created"], "1" if s.get("attached") else "0")
                for s in self.fixture["sessions"]
            )
            return _proc(["tmux", *args], 0, rows, "")
        if args[:1] == ["capture-pane"]:
            name = ""
            if "-t" in args:
                name = args[args.index("-t") + 1].lstrip("=").rstrip(":")
            lines = self.fixture["panes"].get(name)
            if lines is None:
                return _proc(["tmux", *args], 1, "", f"can't find session: {name}")
            return _proc(["tmux", *args], 0, "\n".join(lines) + "\n", "")
        self.unhandled.append(f"tmux {' '.join(args)}")
        return _proc(["tmux", *args], 1, "", "unhandled in the screenshot fixture")

    @staticmethod
    def which(name):
        # tmux has to look present or the board renders its
        # "tmux unavailable" degradation instead of the live session it
        # is here to show. Nothing is ever executed through this path.
        return f"/usr/bin/{name}" if name in ("git", "tmux", "backlog") else None


# ---------------------------------------------------------------------------
# Browser-side determinism
# ---------------------------------------------------------------------------

def _init_script(theme, frozen_ms):
    """Runs before any page script: pins the theme, then freezes the
    clock. The clock matters because three separate labels are written
    from `new Date()` -- the sidebar's "updated <time>", the pane's
    "<n>s ago", and the reply gate's capture age -- and each of them
    differs between two otherwise identical runs."""
    return """
(function () {
  try { window.localStorage.setItem(%(key)s, %(theme)s); } catch (e) {}
  var FIXED = %(fixed)d;
  var RealDate = Date;
  var Frozen = new Proxy(RealDate, {
    construct: function (target, args) {
      return args.length ? new target(...args) : new target(FIXED);
    },
    apply: function () { return new RealDate(FIXED).toString(); }
  });
  RealDate.now = function () { return FIXED; };
  window.Date = Frozen;
})();
""" % {
        "key": json.dumps(THEME_STORAGE_KEY),
        "theme": json.dumps(theme),
        "fixed": frozen_ms,
    }


# Everything that moves after the page has settled, stopped in one place:
# every timer (so no poll repaints mid-shot), every transition and
# animation (a toggle knob mid-slide reads as a real regression in a
# diff), the caret, focus rings, and the two labels whose text comes from
# the SERVER's clock rather than the frozen browser one.
FREEZE_JS = """() => {
  var maxTimer = setTimeout(function () {}, 0);
  for (var i = 1; i <= maxTimer; i++) { clearTimeout(i); clearInterval(i); }
  var style = document.createElement('style');
  style.textContent =
    '*, *::before, *::after { transition: none !important; animation: none !important; ' +
    'scroll-behavior: auto !important; caret-color: transparent !important; }';
  document.head.appendChild(style);
  if (document.activeElement && document.activeElement.blur) document.activeElement.blur();
  var countdown = document.getElementById('countdown');
  if (countdown) countdown.textContent = 'next refresh in 10s';
  var age = document.getElementById('drawer-pane-age');
  if (age) {
    age.textContent = 'captured ' + new Date().toLocaleTimeString() + ' \\u00b7 just now';
    age.className = 'drawer-pane-age';
  }
  return true;
}"""


def freeze(page):
    page.evaluate(FREEZE_JS)
    # One animation frame so the injected stylesheet is in effect before
    # the shot is taken.
    page.wait_for_timeout(120)


def wait_for_board(page, expected_cards):
    page.wait_for_selector("#board .card")
    page.wait_for_function(
        "n => document.querySelectorAll('#board .card').length === n", arg=expected_cards
    )
    page.wait_for_function(
        "() => { var el = document.getElementById('last-updated');"
        " return !!el && el.textContent.trim().length > 0; }"
    )
    page.wait_for_selector("#sessions-list .session-row")


def open_drawer(page, title):
    card = page.locator("#board .card").filter(has_text=title).first
    card.click(position={"x": 10, "y": 10})
    page.wait_for_selector('#drawer[aria-hidden="false"]')
    page.wait_for_selector(".drawer-branch-section")
    page.wait_for_function(
        "() => { var pre = document.getElementById('drawer-pane-pre');"
        " return !!pre && !pre.classList.contains('empty') && pre.textContent.length > 0; }"
    )


# ---------------------------------------------------------------------------
# Shots
# ---------------------------------------------------------------------------

def shoot(context_factory, shot, out_dir, expected_cards, Image):
    name = shot["name"]
    path = os.path.join(out_dir, f"{name}.png")
    context = context_factory(shot["theme"])
    try:
        page = context.new_page()
        page.goto(shot["url"], wait_until="domcontentloaded")
        wait_for_board(page, expected_cards)

        if shot["kind"] in ("drawer", "theater"):
            open_drawer(page, shot["cardTitle"])
            # The frame the committed image has: the branch panel at the
            # top of the drawer body with the live pane below it, so the
            # branch truth and the session read as one thing.
            page.evaluate(
                "() => { var el = document.querySelector('.drawer-branch-section');"
                " if (el) el.scrollIntoView({block: 'start'}); }"
            )
        if shot["kind"] == "theater":
            page.click("#drawer-pane-theater-toggle")
            page.wait_for_selector("#theater.open")
            page.wait_for_timeout(250)

        freeze(page)
        page.screenshot(path=path)
    finally:
        context.close()

    quantize(path, Image)
    return path


def quantize(path, Image):
    """Down to the 256-colour palette the committed images already use.
    Deterministic (median cut, no dithering), and about a third of the
    size of the truecolour PNG the browser hands back -- there is no
    pngquant/optipng to lean on here."""
    dither = getattr(Image, "Dither", Image).NONE
    with Image.open(path) as im:
        im.convert("RGB").quantize(colors=256, dither=dither).save(path, optimize=True)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def build_parser():
    parser = argparse.ArgumentParser(
        prog="scripts/screenshots.py",
        description="Regenerate docs/img/*.png from committed synthetic data.",
        epilog=(
            "There is deliberately no option to point this at a real board or "
            "a real projects.json: the images are published, and a screenshot "
            "of a private repository is the one leak scripts/scan_release.py "
            "cannot catch. See the module docstring."
        ),
    )
    parser.add_argument(
        "--out", default=DEFAULT_OUT_DIR, metavar="DIR",
        help="where to write the PNGs (default: docs/img). Useful for shooting "
             "into a scratch directory and diffing before overwriting.")
    parser.add_argument(
        "--only", action="append", metavar="NAME", default=None,
        help="shoot only this image (repeatable): board-light, board-dark, "
             "drawer-pane, session-theater.")
    parser.add_argument(
        "--keep", action="store_true",
        help="leave the temp sandbox in place for inspection.")
    parser.add_argument(
        "--verbose", action="store_true",
        help="print the sandbox path and the served port.")
    return parser


def parse_args(argv):
    return build_parser().parse_args(argv)


def main(argv=None):
    args = parse_args(argv if argv is not None else sys.argv[1:])
    sync_playwright = require_playwright()
    Image = require_pillow()

    with open(FIXTURE_PATH, "r", encoding="utf-8") as f:
        fixture = json.load(f)

    shots = fixture["shots"]
    if args.only:
        wanted = set(args.only)
        unknown = wanted - {s["name"] for s in shots}
        if unknown:
            _fatal(f"unknown shot(s): {', '.join(sorted(unknown))}")
        shots = [s for s in shots if s["name"] in wanted]

    os.makedirs(args.out, exist_ok=True)
    sandbox = tempfile.mkdtemp(prefix="centrale-screenshots-")
    # Before importing server: hooks_settings_path() and browser.py's
    # registry both resolve under XDG_CACHE_HOME, and neither may land in
    # a real ~/.cache during a screenshot run.
    os.environ["XDG_CACHE_HOME"] = os.path.join(sandbox, "cache")

    sys.path.insert(0, REPO_ROOT)
    import server  # noqa: E402  (imported after the sandbox env is set)

    world = World(fixture, sandbox)
    world.build()
    config = world.config()

    # Belt and braces on top of the boundary guard: even a code path that
    # went looking for the config file finds a sandbox path that does not
    # exist, never the maintainer's projects.json.
    server.DEFAULT_CONFIG_PATH = os.path.join(sandbox, "projects.json")
    server.run_backlog = world.run_backlog
    server.run_backlog_raw = world.run_backlog_raw
    server.run_git = world.run_git
    server.run_tmux = world.run_tmux
    server.which = world.which

    for session in fixture["sessions"]:
        server.record_agent_event(
            session["project"], session["task"], session["agentState"],
            agent_kind=session.get("agentKind"),
        )

    httpd = server.CentraleHTTPServer(("127.0.0.1", 0), server.Handler, config)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{port}/"
    if args.verbose:
        print(f"sandbox: {sandbox}")
        print(f"serving: {url}")

    expected_cards = sum(len(p["tasks"]) for p in fixture["projects"])
    frozen_ms = int(fixture["clock"]["pageNowEpoch"]) * 1000
    executable = os.environ.get("CENTRALE_SCREENSHOT_CHROMIUM") or None
    written = []

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(executable_path=executable)
            try:
                def context_factory(theme):
                    context = browser.new_context(
                        viewport=dict(VIEWPORT),
                        device_scale_factor=DEVICE_SCALE_FACTOR,
                        # Pinned so the same machine cannot produce two
                        # different renderings of the same fixture -- and
                        # so two machines agree.
                        timezone_id="UTC",
                        locale="en-US",
                        color_scheme=theme,
                        reduced_motion="reduce",
                    )
                    context.add_init_script(_init_script(theme, frozen_ms))
                    return context

                for shot in shots:
                    spec = dict(shot)
                    spec["url"] = url
                    spec.setdefault("cardTitle", _card_title(fixture, shot))
                    written.append(shoot(context_factory, spec, args.out, expected_cards, Image))
                    print(f"wrote {_display_path(written[-1])}")
            finally:
                browser.close()
    finally:
        httpd.shutdown()
        httpd.server_close()
        if args.keep:
            print(f"sandbox kept at {sandbox}")
        else:
            shutil.rmtree(sandbox, ignore_errors=True)

    if world.unhandled:
        print(
            "warning: the server made calls this fixture does not answer, so a "
            "shot may be showing a degraded state:", file=sys.stderr)
        for call in sorted(set(world.unhandled)):
            print(f"  {call}", file=sys.stderr)
    if world.refusals:
        print(
            "REFUSED calls outside the screenshot sandbox -- the images above "
            "are not trustworthy:", file=sys.stderr)
        for call in sorted(set(world.refusals)):
            print(f"  {call}", file=sys.stderr)
        return 1
    return 0


def _display_path(path):
    """Repo-relative when it is inside the repo, absolute otherwise --
    a `--out` in /tmp should not print as a stack of `../`."""
    real = os.path.realpath(path)
    root = os.path.realpath(REPO_ROOT)
    if real.startswith(root + os.sep):
        return os.path.relpath(real, root)
    return real


def _card_title(fixture, shot):
    """The card a drawer/theater shot opens, looked up from the fixture
    by project + task id so the shot spec names ids, not prose."""
    if shot["kind"] not in ("drawer", "theater"):
        return None
    project = next(p for p in fixture["projects"] if p["name"] == shot["project"])
    return next(t["title"] for t in project["tasks"] if t["id"] == shot["task"])


if __name__ == "__main__":
    raise SystemExit(main())
