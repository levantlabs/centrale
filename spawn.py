"""Worktree + tmux spawn engine for `POST /api/spawn` and its resume
variant, `POST /api/resume` (see resume(); with `"reconcile": true` it
is the task-66 resume-to-reconcile variant).

Imported lazily by server.py (inside the request handler) to avoid a
circular import, since this module imports `server` for its subprocess
boundary functions (`run_git`, `run_tmux`, `list_sessions`) and shared
constants (`TASK_ID_RE`, `SESSION_PREFIX`).

Python 3.12 stdlib only. All calls into `server` go through the module
object at call time (`server.run_git(...)`, not `from server import
run_git`) so tests can patch `server.run_git` / `server.run_tmux` /
`server.list_sessions` in one place and have both modules see the patch.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shlex
import shutil
import tempfile
import time
import urllib.parse

import server

# The built-in spawn prompt. Overridable per install via the top-level
# projects.json "spawnPrompt" key (see server.normalize_spawn_prompt and
# prompt_for below); an agent's promptSuffix is appended on top of
# whichever template is active. The decision-records sentence (task-69)
# is deliberate: `backlog decision list --plain` is the durable "why"
# layer agents otherwise only find through lucky search-term overlap,
# and on a repo with none it prints "No decisions found." and costs
# nothing more than that one command.
PROMPT_TEMPLATE = (
    "Work on backlog task {task_id}. Follow the Backlog.md workflow: run "
    "`backlog instructions overview` first, claim the task, implement it, "
    "check off acceptance criteria as you meet them, add implementation "
    "notes, and update the status when done. Before designing anything, "
    "check the repo's standing decision records (`backlog decision list "
    "--plain`) and treat them as constraints. When you are done, commit "
    "all your work on this branch, including the backlog task updates. "
    "Do NOT merge this branch into the default branch or delete it -- "
    "the dashboard's gated merge handles integration."
)


class SpawnError(Exception):
    """Raised for any spawn failure. `status` is the HTTP status the
    server should respond with (400/404 validation, 409 duplicate session,
    500/502 git/tmux failure)."""

    def __init__(self, message, status=400):
        super().__init__(message)
        self.message = message
        self.status = status

    def __str__(self):
        return self.message


# task-113: the geometry every session Centrale creates is born with,
# as (columns, rows). `tmux new-session -d` with no -x/-y inherits the
# `default-size` option -- 80x24 out of the box -- and that ceiling is
# the whole reason the session theater only ever had a screenful to
# show. Claude Code and Codex are full-screen TUIs running on tmux's
# alternate screen, where output never scrolls off the top into
# scrollback; the TUI redraws a fixed viewport instead. So the pane's
# history stays empty (measured: history_size=0 against a 50,000-line
# history-limit) and `capture-pane -S -200` can only ever hand back the
# visible height, however many lines the theater asks for. Raising the
# viewport is the only way to raise what a capture can contain.
#
# Deliberately one module constant and not a projects.json key: nothing
# here is per-project, and an unused knob is worse than a number with a
# comment. 220 columns is wide enough that an 80-column TUI has room for
# its own side-by-side layouts.
#
# task-152: the row count is not a second, independent number. Because a
# capture can only ever return the pane's height (measured on an
# alternate-screen TUI at 50, 100 and 200 rows: the capture came back
# with exactly 50, 100 and 200 lines), a pane shorter than the largest
# window /api/session-pane will hand back is a UI asking for lines that
# can never arrive -- which is what 50 rows against the theater's 200
# line request was. So the pane is built exactly as tall as that
# ceiling, and the two move together or not at all.
#
# The cost is `tmux attach`, and it is real: the pin below already means
# an attached terminal smaller than this sees only the part of the
# viewport that fits, and at 200 rows that is most terminals, showing a
# corner of the window rather than the agent's composer at the bottom.
# The theater is the way Centrale is meant to watch a session and attach
# the occasional one (task-113 accepted that trade at 50 rows; this
# deepens it), and the per-session undo in docs/agents.md gives a
# terminal its own size back in one command.
SESSION_GEOMETRY = (220, server.MAX_SESSION_PANE_LINES)


# tmux forbids "." in a session name -- it's the window/pane-index
# separator in a -t target -- and silently rewrites it out from under a
# caller who asks for one anyway: `new-session -s foo-11.2` actually
# creates a session named "foo-11_2", and `attach -t foo-11.2` then
# fails with "can't find pane: 2" (both verified empirically, task-59).
# Since a Backlog.md subtask id always contains a dot (TASK-11.2), every
# name-based consumer of a spawned session -- the duplicate-session 409
# check, the sessions sidebar's session->task linking, and harvest's
# noLiveSession merge gate -- must encode/decode through the same pair
# of helpers below rather than embedding task_id.lower() directly, or
# they silently miss the session tmux actually created.
_ENCODED_TASK_ID_RE = re.compile(r"^[a-z]+-[0-9]+(?:_[0-9]+)*$")


def encode_task_id_for_session(task_id):
    """tmux-safe encoding of `task_id` for embedding in a session name:
    lowercased, with every "." replaced by "_" (tmux accepts "_" in a
    session name verbatim -- verified empirically, no further mangling).
    The single place a task id becomes part of a session name -- see
    session_name() below, the only caller.
    """
    return task_id.lower().replace(".", "_")


def is_encoded_task_id(candidate):
    """True if `candidate` has the shape encode_task_id_for_session()
    produces (e.g. "task-11_2") -- used by the session-name -> task-id
    reverse mapping (server._parse_session_project_and_task) to
    recognize the task-id portion of a session name before decoding it
    with decode_session_task_id. A real task id (server.TASK_ID_RE) can
    only contain letters, digits, "-", and ".", never "_" -- so this
    match is unambiguous: no real task id's encoded form collides with
    anything else this could match, and nothing that matches this could
    have come from a task id containing a literal "_" (there is no such
    task id to begin with)."""
    return bool(_ENCODED_TASK_ID_RE.match(candidate))


def decode_session_task_id(encoded):
    """Reverse of encode_task_id_for_session, for a string already
    confirmed to match is_encoded_task_id -- e.g. "task-11_2" ->
    "task-11.2". Performs no validation of its own; callers must check
    is_encoded_task_id first. Because encode_task_id_for_session's only
    transform is "." -> "_" and real task ids never contain "_" (see
    is_encoded_task_id), this is a lossless inverse -- not a hopeful
    blind reversal -- for anything that actually matched."""
    return encoded.replace("_", ".")


def session_name(project, task_id):
    return f"{server.SESSION_PREFIX}{project}-{encode_task_id_for_session(task_id)}"


WORKTREE_DIRNAME = ".centrale-worktrees"


def worktree_dir(config, project, task_id):
    """THE single place every worktree path is derived (spawn, resume,
    harvest's gate evaluation, board's worktreeDirty, sessions'
    touched-files) -- always go through this, never read
    config["worktreeRoot"] directly, so the '@repo' in-repo-worktree
    resolution (see resolve_worktree_root) can never drift out of sync
    between callers."""
    root = resolve_worktree_root(config, project)
    return os.path.join(root, f"{project}-{task_id.lower()}")


def resolve_worktree_root(config, project_name):
    """The real, absolute directory `project_name`'s worktrees live
    under: that project's own 'worktreeRoot' override if it set one,
    else the global 'worktreeRoot' -- with server.WORKTREE_ROOT_REPO_TOKEN
    ('@repo'), from either source, resolved to
    `<project path>/.centrale-worktrees` so a spawned worktree is a
    filesystem descendant of the project itself (see `worktreeRoot`
    in docs/configuration.md for why that matters to codex's own directory-
    trust sandboxing). A plain path -- the traditional shared-sibling-
    directory default -- passes through unchanged, already ~-expanded
    by server.load_config."""
    project = _find_project(config, project_name)
    per_project_root = project.get("worktreeRoot") if project else None
    root = per_project_root or config.get("worktreeRoot")
    if root == server.WORKTREE_ROOT_REPO_TOKEN:
        if project is None:
            raise ValueError(f"cannot resolve '@repo' worktreeRoot for unknown project {project_name!r}")
        return os.path.join(project["path"], WORKTREE_DIRNAME)
    return root


def branch_name(task_id):
    return f"task/{task_id.lower()}"


def spawn_cmd_override():
    """The raw ``CENTRALE_SPAWN_CMD`` test-override string, if set.
    Every override gate in this module goes through this one function
    rather than reading the env var directly, so they can never drift
    out of sync with each other."""
    return os.environ.get("CENTRALE_SPAWN_CMD")


def spawn_cmd():
    """The agent command to launch, as an argv list. Defaults to
    ``["claude"]``; overridable via ``CENTRALE_SPAWN_CMD`` (shlex-split
    -- see spawn_cmd_override) so tests can point at a harmless probe
    instead of a real agent."""
    raw = spawn_cmd_override()
    if not raw:
        return ["claude"]
    parsed = shlex.split(raw)
    return parsed or ["claude"]


def prompt_template(config=None):
    """The active spawn prompt template: the config's top-level
    "spawnPrompt" (task-69; already validated by server.load_config to
    carry exactly the {task_id} placeholder) when set, else the built-in
    PROMPT_TEMPLATE. Tolerant of a config dict that never went through
    load_config (tests build them by hand), and of no config at all."""
    if config:
        override = config.get("spawnPrompt")
        if override:
            return override
    return PROMPT_TEMPLATE


def prompt_for(task_id, config=None):
    """The spawn prompt for task_id, from prompt_template(config). This is
    the *template* layer only -- an agent's promptSuffix is appended by
    the callers (spawn(), resume()'s fresh-start fallback) on top of it,
    identically for the default and an override."""
    return prompt_template(config).format(task_id=task_id)


def agent_kind_for_command(cmd):
    """Return the built-in lifecycle family for ``cmd``, or None.

    This intentionally uses the executable basename, exactly like
    _inject_agent_hooks: configured names such as ``claude-sonnet`` are
    still claude-family when they launch ``claude``, while an arbitrary
    wrapper is not assigned semantics Centrale cannot verify.
    """
    if not cmd:
        return None
    basename = os.path.basename(cmd[0])
    return basename if basename in server.AGENT_KINDS else None


def event_url(config, project_name, task_id, agent_kind=None):
    """The CENTRALE_EVENT_URL set (via tmux -e) on every spawned/resumed
    session's own environment, regardless of agent type or the
    CENTRALE_SPAWN_CMD test override (task-37 AC #4): identity travels
    entirely in this query string, not in the agent-generic hooks
    settings file _inject_agent_hooks points a claude-family agent at --
    so the same one file works for every task, and a hook-less custom
    agent that simply POSTs here on its own participates in agentState
    reporting with zero injection at all. Built-in agents also carry
    ``agentKind`` so the server can honestly derive codex turn-end as
    idle; the parameter is omitted for custom agents and legacy callers.
    """
    port = config.get("port") or 7420
    params = {"project": project_name, "task": task_id}
    if agent_kind is not None:
        params["agentKind"] = agent_kind
    query = urllib.parse.urlencode(params)
    return f"http://127.0.0.1:{port}/api/agent-event?{query}"


def _inject_codex_hooks(codex_argv0):
    """The extra argv codex gets from _inject_agent_hooks: the -c notify
    override, always -- belt-and-suspenders for, and the ONLY thing that
    happens on, a codex without the hooks engine (see notify_argv below)
    -- plus, only on a codex that actually supports it, the same four
    hook definitions as inline -c config overrides (see
    server.codex_hooks_overrides) and --dangerously-bypass-hook-trust so
    codex's per-source hook-trust prompt doesn't block a detached spawn:
    this is the flag's own documented use case (automation that already
    vets its hook sources -- centrale generates the override values
    itself, nothing user- or repo-supplied ever lands in them).

    task-44: this used to write a full-fidelity hooks.json into the
    worktree instead (<wt_dir>/.codex/hooks.json, git-excluded). Live
    debugging of a stale "finished" badge on another repo's TASK-2
    found why that
    never actually worked: codex resolves its *project* config layer
    through a linked worktree's `.git` FILE to the MAIN repo root, not
    the worktree itself -- so a worktree-local .codex/hooks.json is
    NEVER discovered in exactly the environment centrale spawns agents
    into. Proven empirically: an identical hooks.json + flags fired the
    full working/waiting/finished event stream from a plain directory
    under a trusted root, and fired nothing at all (notify-only) from a
    real linked worktree; passing the same four hook definitions as -c
    overrides on the argv itself -- which never touch the filesystem, so
    there is no config *layer* for codex to resolve wrong -- fired the
    full stream FROM a linked worktree. See "Agent lifecycle
    events" in docs/agents.md for
    the full writeup; this is exactly why no file write or git-exclude
    step remains here at all.

    Whether this codex binary supports the flag (and therefore the
    overrides -- both gated together, since a codex too old to know
    --dangerously-bypass-hook-trust would also reject syntax it doesn't
    recognize) is determined by server.probe_codex_hook_trust(codex_argv0).
    An older codex (< 0.150.0) doesn't recognize
    --dangerously-bypass-hook-trust and rejects the whole argv at
    startup, killing the spawned session outright -- so this must know
    in advance, before ever appending it, never find out by trying. A
    probe failure (an exception, not just a negative result) is treated
    the same as a negative probe: notify-only, same as an old codex.
    """
    notify_argv = ["python3", server.notify_script_path(), "finished"]
    notify_override = ["-c", f"notify={json.dumps(notify_argv)}"]
    try:
        supports_hooks = server.probe_codex_hook_trust(codex_argv0)
    except Exception:
        supports_hooks = False
    if not supports_hooks:
        return notify_override
    return [*notify_override, *server.codex_hooks_overrides(), "--dangerously-bypass-hook-trust"]


def _inject_agent_hooks(cmd):
    """Extra argv to append to `cmd` (the resolved agent argv), right
    before any prompt argument, so the spawned agent reports its own
    lifecycle back to CENTRALE_EVENT_URL. Dispatched on
    os.path.basename(cmd[0]):

    - "claude" gets ["--settings", <generated hooks settings file>] --
      see server.ensure_hooks_settings_file. Covers any claude-family
      entry whose underlying binary is literally "claude" (e.g.
      "claude-sonnet": ["claude", "--model", "sonnet"]), same basename
      check resume() already uses to decide "claude family".
    - "codex" gets four inline -c hook overrides (see
      _inject_codex_hooks/server.codex_hooks_overrides) when
      server.probe_codex_hook_trust(cmd[0]) says this binary is
      >= 0.150.0 and supports --dangerously-bypass-hook-trust, reporting
      working/waiting/finished exactly like claude does, plus the
      belt-and-suspenders -c notify override (task-37) that alone covers
      an older codex without the hooks engine -- see _inject_codex_hooks
      for the full breakdown, including task-44's linked-worktree finding.
    - anything else gets nothing: CENTRALE_EVENT_URL alone (see
      event_url, always set regardless) is the entire contract for a
      custom agent that chooses to honor it.

    Shared by spawn() and resume() so both inject identically (AC #6).
    Never raises: if the claude hooks settings file can't be generated
    (e.g. a read-only cache dir), that claude-family spawn still starts,
    just without agentState reporting, the same best-effort spirit as
    every other degradation in this module.
    """
    if not cmd:
        return []
    basename = os.path.basename(cmd[0])
    if basename == "claude":
        try:
            settings_path = server.ensure_hooks_settings_file()
        except OSError:
            return []
        return ["--settings", settings_path]
    if basename == "codex":
        return _inject_codex_hooks(cmd[0])
    return []


def _fetch_task(project, task_id):
    """Fetches `backlog task view <task_id> --json`'s "task" dict for
    `project`, via the server.run_backlog boundary. Returns {} if the CLI
    call fails or returns malformed data -- never raises. Shared by the
    pre-claim Done-status check (see spawn()/resume()) and resolve_agent's
    assignee lookup below, so a single spawn/resume call only ever makes
    one `backlog task view` CLI call, not two.
    """
    try:
        data = server.run_backlog(["task", "view", task_id, "--json"], cwd=project["path"])
    except server.BacklogError:
        return {}
    if isinstance(data, dict):
        return data.get("task") or {}
    return {}


def resolve_agent(config, project, task_id, task=None):
    """Resolve which agent should run this task, from the first assignee
    on `backlog task view <task_id>` (leading '@' stripped, matched
    case-insensitively against config['agents']). Falls back to
    config['defaultAgent'] when there's no assignee, no match, or the
    backlog CLI call fails. Never raises.

    `task` is the already-fetched task dict (see _fetch_task) when the
    caller has one on hand -- spawn()/resume() fetch it once for the
    pre-claim Done-status check and pass it through here so this doesn't
    make a second, redundant `backlog task view` call. Fetches it itself
    (unchanged behavior) when not given one, e.g. when called directly.

    Normalization (accepting either a plain argv list or a `{"cmd": [...],
    "promptSuffix": "...", "resumeCmd": [...]}` object, and rejecting a
    malformed entry) is entirely `server.normalize_agents_map`'s job,
    applied once at config-load time (`server.load_config`) — this
    function trusts `config['agents']` is already in that one canonical
    shape (`{name: {"cmd": [...], "promptSuffix": str | None,
    "resumeCmd": [...] | None}}`) and only falls back to a normalized
    DEFAULT_AGENTS when it's missing or empty.

    Returns (agent_name, argv, prompt_suffix) where prompt_suffix is the
    string to append to the standard workflow prompt, or None. The
    canonical entry's third field, `resumeCmd`, is deliberately not
    returned here: only resume() wants it, and it reads it off the same
    map through resume_cmd_for_agent rather than widening this
    already-tested 3-value return.
    """
    agents = config.get("agents") or server.normalize_agents_map(server.DEFAULT_AGENTS)
    agents_lc = {str(key).lower(): value for key, value in agents.items()}

    default_agent = str(config.get("defaultAgent") or server.DEFAULT_AGENT_NAME).lower()
    if default_agent not in agents_lc:
        default_agent = (
            server.DEFAULT_AGENT_NAME if server.DEFAULT_AGENT_NAME in agents_lc else next(iter(agents_lc))
        )

    if task is None:
        task = _fetch_task(project, task_id)

    assignee = None
    assignees = task.get("assignees") or []
    if assignees:
        assignee = str(assignees[0]).lstrip("@").strip().lower()

    agent_name = assignee if assignee in agents_lc else default_agent
    entry = agents_lc[agent_name]
    return agent_name, list(entry["cmd"]), entry.get("promptSuffix")


def _find_project(config, project_name):
    for project in config.get("projects", []):
        if project.get("name") == project_name:
            return project
    return None


def _branch_exists(cwd, branch):
    proc = server.run_git(
        ["rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"], cwd=cwd
    )
    return proc.returncode == 0


def _worktree_registered(cwd, wt_dir):
    proc = server.run_git(["worktree", "list", "--porcelain"], cwd=cwd)
    if proc.returncode != 0:
        return False
    target = os.path.realpath(wt_dir)
    for line in (proc.stdout or "").splitlines():
        if line.startswith("worktree "):
            candidate = line[len("worktree ") :].strip()
            if os.path.realpath(candidate) == target:
                return True
    return False


def parse_worktree_checkouts(porcelain_output):
    """Maps each checked-out branch (short name, e.g. "task/task-7") to
    the worktree path holding it, parsed from `git worktree list
    --porcelain` output: one blank-line-separated block per worktree,
    "worktree <path>" first, then "HEAD <sha>", then "branch
    refs/heads/<name>" -- or "detached"/"bare" instead, which carry no
    branch and are skipped. Git allows a branch in at most one worktree,
    so the first block naming it wins (task-70)."""
    checkouts = {}
    path = None
    for line in (porcelain_output or "").splitlines():
        if line.startswith("worktree "):
            path = line[len("worktree "):].strip()
        elif line.startswith("branch ") and path is not None:
            ref = line[len("branch "):].strip()
            if ref.startswith("refs/heads/"):
                ref = ref[len("refs/heads/"):]
            checkouts.setdefault(ref, path)
        elif not line.strip():
            path = None
    return checkouts


def branch_checkouts(repo_path):
    """{branch: worktree path} for every branch checked out anywhere in
    `repo_path`'s worktree set -- Centrale's own worktrees, the main
    checkout, and any foreign one (.worktrees/, .claude/worktrees/, a
    /tmp checkout, ...) alike. ONE `git worktree list --porcelain` call;
    a git failure reads as "nothing checked out" ({}), the same fail-
    open spirit as the board's other display hints (task-70)."""
    proc = server.run_git(["worktree", "list", "--porcelain"], cwd=repo_path)
    if proc.returncode != 0:
        return {}
    return parse_worktree_checkouts(proc.stdout)


def _now():
    """Wall clock, as a function so tests can pin it."""
    return time.time()


def branch_last_commit(repo_path, branch):
    """(iso_utc, age_seconds) of `branch`'s tip commit -- the freshness
    signal for a branch worked outside Centrale (task-70) -- or (None,
    None) when git can't say."""
    proc = server.run_git(["log", "-1", "--format=%ct", branch], cwd=repo_path)
    if proc.returncode != 0:
        return None, None
    try:
        committed = int((proc.stdout or "").strip())
    except ValueError:
        return None, None
    iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(committed))
    return iso, max(0, int(_now()) - committed)


def checkout_state(config, project, task_id, checkouts=None):
    """Where task_id's task/<id> branch is checked out, derived purely
    from git (task-70): {"kind": "centrale" | "external" | "none",
    "path": <worktree path or None>} -- plus "lastCommitAt" (ISO 8601
    UTC) and "lastCommitAgeSeconds" for the external kind only, the
    freshness signal the board shows for work happening outside
    Centrale's view.

      centrale  -- checked out at the Centrale-managed worktree path
                   (worktree_dir), whether or not that directory still
                   exists on disk (git still lists a hand-deleted one).
      external  -- checked out in some OTHER worktree: the branch was
                   adopted into a foreign checkout and work may be
                   continuing there. git refuses a second checkout of
                   the same branch, so spawn/resume must refuse too.
      none      -- the branch exists but is checked out nowhere: a
                   "parked" branch (the companion worktree-less merge
                   task's subject), deliberately distinct from external.

    `project` is the project config dict. `checkouts` lets the board
    pass one project-wide branch_checkouts() result across every task
    instead of re-listing per task."""
    if checkouts is None:
        checkouts = branch_checkouts(project["path"])
    branch = branch_name(task_id)
    path = checkouts.get(branch)
    if path is None:
        return {"kind": "none", "path": None}
    wt_dir = worktree_dir(config, project["name"], task_id)
    if os.path.realpath(path) == os.path.realpath(wt_dir):
        return {"kind": "centrale", "path": path}
    last_at, age = branch_last_commit(project["path"], branch)
    return {
        "kind": "external",
        "path": path,
        "lastCommitAt": last_at,
        "lastCommitAgeSeconds": age,
    }


def external_checkout_reason(branch, path):
    """The one sentence every refusal about an externally checked-out
    branch shares (spawn/resume 409, the drawer's disabled action)."""
    return (
        f"{branch} is checked out outside Centrale at {path} -- git refuses a "
        f"second checkout of the same branch; finish or remove that worktree first"
    )


@contextlib.contextmanager
def detached_snapshot(repo_path, ref):
    """A throwaway *detached* worktree holding `ref`'s exact committed
    tree, for reading a branch Centrale must not check out.

    Yields ``(path, error)``: the snapshot directory and None, or
    ``(None, "<git stderr>")`` when git refused to create it (an
    unknown ref, a broken repo). Detached means `ref` itself is never
    checked out and therefore never reserved, so this is safe for a
    parked branch a spawn might claim a moment later. Reading through
    a real tree -- rather than `git show ref:path` -- keeps the Backlog
    CLI the only task-data boundary and lets it resolve its own task
    filename, spaces and all.

    ``tempfile`` honors TMPDIR, and both the git registration and the
    directory are removed on every path, including the failed-add one
    (a partial add can still leave a registration behind).

    Shared by harvest's gate 2 (a parked branch's committed task) and
    GET /api/task's "branchTask" for the same branch -- one dance, so
    the two can never disagree about what a parked branch says."""
    tmp_dir = tempfile.mkdtemp(prefix="centrale-snapshot-")
    # `git worktree add` wants to create the directory itself.
    os.rmdir(tmp_dir)
    try:
        add_proc = server.run_git(
            ["worktree", "add", "--detach", tmp_dir, ref], cwd=repo_path
        )
        if add_proc.returncode != 0:
            stderr = (add_proc.stderr or add_proc.stdout or "unknown error").strip()
            yield None, stderr
        else:
            yield tmp_dir, None
    finally:
        server.run_git(["worktree", "remove", "--force", tmp_dir], cwd=repo_path)
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _check_not_checked_out_externally(config, project, task_id):
    """Raises SpawnError(409) when the task's branch lives in a foreign
    worktree (see checkout_state), naming that checkout -- run by
    spawn() and resume() before ANY side effect (claim commit, worktree
    add), so the honest reason replaces the raw `git worktree add`
    error ("'task/x' is already checked out at ...") that used to
    surface only after the claim had already been committed (task-70).
    Pure git; runs under the CENTRALE_SPAWN_CMD override too."""
    state = checkout_state(config, project, task_id)
    if state["kind"] == "external":
        raise SpawnError(external_checkout_reason(branch_name(task_id), state["path"]), status=409)


def current_branch(cwd):
    """Best-effort current branch of the repo's HEAD, for use as the base
    of a new worktree branch (and, from harvest.py, the branch a
    finished task branch merges back into). Falls back to "HEAD" if
    detached. Public (no leading underscore) since harvest.py reuses it
    directly, unlike this module's other worktree-internals helpers."""
    proc = server.run_git(["symbolic-ref", "--short", "HEAD"], cwd=cwd)
    if proc.returncode == 0 and (proc.stdout or "").strip():
        return proc.stdout.strip()
    proc = server.run_git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=cwd)
    branch = (proc.stdout or "").strip()
    if proc.returncode == 0 and branch and branch != "HEAD":
        return branch
    return "HEAD"


def _claim_and_commit(project, task_id):
    """Best-effort: claim the task (status -> "In Progress", existing
    assignee left alone) and commit any resulting change under backlog/
    on the project's current branch, before the worktree is cut for it —
    so the worktree's base branch already contains the claim, instead of
    the `backlog` CLI creating a second, divergent copy of the task file
    once the agent claims it again inside the worktree.

    Never raises: a failure at either step degrades to a warning string
    rather than blocking the spawn. Returns a list of such warnings
    (empty when everything succeeded, including the case where there was
    nothing under backlog/ to commit).
    """
    warnings = []
    repo_path = project["path"]

    claim_proc = server.run_backlog_raw(["task", "edit", task_id, "-s", "In Progress"], cwd=repo_path)
    if claim_proc.returncode != 0:
        stderr = (claim_proc.stderr or claim_proc.stdout or "unknown error").strip()
        warnings.append(f"failed to claim {task_id} before spawn: {stderr}")

    status_proc = server.run_git(["status", "--porcelain", "--", "backlog"], cwd=repo_path)
    if status_proc.returncode != 0:
        stderr = (status_proc.stderr or status_proc.stdout or "unknown error").strip()
        warnings.append(f"failed to check backlog status before spawn: {stderr}")
        return warnings
    if not (status_proc.stdout or "").strip():
        return warnings  # nothing under backlog/ changed: skip the commit silently

    add_proc = server.run_git(["add", "--", "backlog"], cwd=repo_path)
    if add_proc.returncode != 0:
        stderr = (add_proc.stderr or add_proc.stdout or "unknown error").strip()
        warnings.append(f"failed to stage backlog changes before spawn: {stderr}")
        return warnings

    commit_proc = server.run_git(["commit", "-m", f"backlog: claim {task_id} for spawn"], cwd=repo_path)
    if commit_proc.returncode != 0:
        stderr = (commit_proc.stderr or commit_proc.stdout or "unknown error").strip()
        warnings.append(f"failed to commit backlog claim for {task_id}: {stderr}")

    return warnings


def _repo_relative_worktree_root(repo_path, worktree_root):
    """`worktree_root`'s path relative to `repo_path`, if it's actually
    inside the repo (the in-repo '@repo' case, or a plain worktreeRoot a
    user happened to point there themselves) -- None if it's somewhere
    else entirely (the traditional sibling-directory default), so
    _ensure_worktree_root_excluded knows there's nothing to exclude."""
    repo_real = os.path.realpath(repo_path)
    root_real = os.path.realpath(worktree_root)
    if repo_real == root_real:
        return None
    prefix = repo_real + os.sep
    if not root_real.startswith(prefix):
        return None
    return root_real[len(prefix):]


def _ensure_worktree_root_excluded(repo_path, worktree_root):
    """Idempotently adds `worktree_root` (as a repo-relative entry, with
    a trailing '/') to this repo's .git/info/exclude when it's inside
    the repo -- untracked-only, unlike the tracked .gitignore, so an
    in-repo worktree root never shows up in `git status` without
    touching a file every clone/collaborator shares. A no-op when
    worktree_root isn't inside the repo at all (the traditional sibling-
    directory default). Best-effort: any failure here (a read-only
    filesystem, a missing .git) never blocks a spawn -- it only degrades
    to a dirty `git status` for that one directory, same as if this
    function didn't exist."""
    relative = _repo_relative_worktree_root(repo_path, worktree_root)
    if relative is None:
        return
    entry = relative.rstrip("/\\").replace(os.sep, "/") + "/"
    exclude_path = os.path.join(repo_path, ".git", "info", "exclude")
    try:
        existing = ""
        if os.path.isfile(exclude_path):
            with open(exclude_path, "r", encoding="utf-8") as f:
                existing = f.read()
        if entry in existing.splitlines():
            return
        os.makedirs(os.path.dirname(exclude_path), exist_ok=True)
        with open(exclude_path, "a", encoding="utf-8") as f:
            if existing and not existing.endswith("\n"):
                f.write("\n")
            f.write(entry + "\n")
    except OSError:
        pass


def _ensure_worktree(config, project, task_id):
    """Create (or reuse) the git worktree for this project+task. Returns
    the worktree directory path. Raises SpawnError(status=500) on an
    unexpected git failure."""
    repo_path = project["path"]
    branch = branch_name(task_id)
    wt_dir = worktree_dir(config, project["name"], task_id)
    worktree_root = os.path.dirname(wt_dir)  # derived from wt_dir, never config["worktreeRoot"] directly

    os.makedirs(worktree_root, exist_ok=True)
    _ensure_worktree_root_excluded(repo_path, worktree_root)

    dir_exists = os.path.isdir(wt_dir)
    registered = _worktree_registered(repo_path, wt_dir) if dir_exists else False

    if dir_exists and registered:
        # Re-spawn after a killed tmux session: reuse the worktree as-is.
        return wt_dir

    branch_exists = _branch_exists(repo_path, branch)

    if branch_exists and not registered:
        proc = server.run_git(["worktree", "add", wt_dir, branch], cwd=repo_path)
    else:
        base = current_branch(repo_path)
        proc = server.run_git(
            ["worktree", "add", "-b", branch, wt_dir, base], cwd=repo_path
        )

    if proc.returncode != 0:
        stderr = (proc.stderr or proc.stdout or "git worktree add failed").strip()
        raise SpawnError(f"git worktree add failed: {stderr}", status=500)

    return wt_dir


def _check_not_done(project, task_id):
    """Fetches the task (see _fetch_task) and raises SpawnError(409) if
    its status is terminal "Done" (case-insensitive) -- shared by spawn()
    and resume() so neither ever claims a Done task back to "In Progress"
    or reuses/creates a worktree for already-finished work (task-41).
    Only "Done" is refused: "In Progress" stays spawnable (the interrupted
    re-spawn/resume path), as does everything else.

    Callers run this before ANY side effect (claim commit, worktree) --
    see spawn()/resume() -- and skip it entirely under the
    CENTRALE_SPAWN_CMD hermetic test override, the same way
    resolve_agent's own backlog CLI call is skipped there, so tests stay
    free to spawn probes onto arbitrary/fixture task ids without a real
    `backlog task view` in the loop.

    Returns the fetched task dict (possibly {}) so callers can pass it to
    resolve_agent and avoid a second, redundant CLI call.
    """
    task = _fetch_task(project, task_id)
    status = str(task.get("status") or "").strip().lower()
    if status == "done":
        raise SpawnError(f"task {task_id} is Done — nothing to spawn", status=409)
    return task


def _validate_and_check_session(config, project_name, task_id):
    """Shared by spawn() and resume(): validates project_name/task_id,
    checks tmux availability, and raises SpawnError(409) if a session
    for this project+task is already live -- identical naming/validation/
    duplicate-session behavior for both. Returns (project, session_name),
    the latter the session name a fresh spawn/resume actually uses.

    Never touches git/tmux beyond the list_sessions() check itself.
    """
    if not isinstance(project_name, str) or not project_name:
        raise SpawnError("missing or invalid project", status=400)
    if not isinstance(task_id, str) or not task_id:
        raise SpawnError("missing or invalid taskId", status=400)

    # Defense in depth: the frontend already disables spawn/resume UI when
    # GET /api/board reports tmux unavailable, but a direct API call (or
    # a stale page) should still get a clear, actionable error rather
    # than a raw "no such file or directory" from tmux itself.
    if not server.tmux_capability(config):
        raise SpawnError("Spawning requires tmux (e.g. sudo apt install tmux)", status=503)

    project = _find_project(config, project_name)
    if project is None:
        raise SpawnError(f"unknown project: {project_name}", status=404)

    if not server.TASK_ID_RE.match(task_id):
        raise SpawnError(f"invalid task id: {task_id}", status=400)

    name = session_name(project_name, task_id)

    try:
        existing = server.list_sessions()
    except server.BacklogError as exc:
        raise SpawnError(f"failed to check existing sessions: {exc}", status=502) from exc

    if name in {s.get("name") for s in existing}:
        raise SpawnError(f"session already exists: {name}", status=409)

    return project, name


def new_session_args(name, wt_dir):
    """The leading `tmux new-session` argv shared by spawn() and
    resume() -- the two, and only two, places Centrale creates a
    session. Both go through here so neither can be given a geometry
    the other doesn't have (task-113); callers append their own -e
    pairs, the agent command and its prompt after it."""
    columns, rows = SESSION_GEOMETRY
    return [
        "new-session", "-d", "-s", name, "-c", wt_dir,
        "-x", str(columns), "-y", str(rows),
    ]


def hold_session_geometry(name):
    """Put a just-created session AT SESSION_GEOMETRY and hold it there
    for the rest of its life (task-113, corrected by task-152).

    Two tmux calls, and both are load bearing.

    The pin -- `window-size manual` -- is what stops a LATER attach from
    undoing the size. `window-size` defaults to `latest`, so the first
    client to attach renegotiates the window down to its own terminal
    and -- this is the part that matters -- it STAYS there after that
    client detaches (measured on tmux 3.4: a 220x50 session attached
    from a 100x24 terminal reads 100x23 both during the attach and
    after it).

    The resize behind it is what makes the size right in the first
    place. `window-size latest` is inherited from the global option, so
    it applies to a brand-new DETACHED session too, and it is resolved
    at birth -- before anything here can set `manual` on the session.
    Whenever any client is attached anywhere on the tmux server, the
    -x/-y from new_session_args() is therefore overridden the moment
    the session is created, and pinning alone freezes the WRONG size
    rather than preventing it. Reproduced on an isolated socket with a
    142x30 client attached (task-152):

        new-session -d -s probe -x 220 -y 50   -> 142x29   overridden
        set-option -t =probe: window-size manual -> 142x29 pinned wrong
        resize-window -t =probe: -x 220 -y 50  -> 220x50   this is what lands

    That is why the original passed review and testing and failed in
    real use: a clean socket has no clients, and the owner's terminal is
    always attached to the same tmux server the agents are spawned on.

    The trade-off, accepted deliberately: an attached user whose
    terminal is smaller than SESSION_GEOMETRY sees only the part of the
    viewport that fits, and has to move around it rather than seeing
    the whole pane at once. The theater is the primary way Centrale is
    meant to watch a session and `tmux attach` the occasional one, so
    the geometry wins. Anyone who wants their terminal's size back for
    one session can undo it by hand:
    `tmux set-option -t <session>: window-size latest`.

    Both calls are best-effort by design: the session is already created
    and the agent already running by the time this runs, so a tmux that
    knows neither command (both arrived in the 2.9/3.0 generation) must
    cost the caller a smaller pane, not a failed spawn. Both return
    codes are ignored for that reason, and the resize is issued even if
    the pin failed -- an unpinned session at the right size is still
    better than a pinned one at the wrong size.
    """
    columns, rows = SESSION_GEOMETRY
    target = f"={name}:"
    server.run_tmux(["set-option", "-t", target, "window-size", "manual"])
    server.run_tmux(["resize-window", "-t", target, "-x", str(columns), "-y", str(rows)])


def spawn(config, project_name, task_id):
    """Refuses a task whose status is Done with a 409 (see
    _check_not_done), before any side effect. Otherwise claims the task
    and commits that claim (see _claim_and_commit), creates/reuses a
    worktree from the resulting base-branch commit, and starts a detached
    tmux session running the resolved agent (see resolve_agent) with the
    standard task prompt, plus that agent's promptSuffix if it has one.
    The session's own environment always carries CENTRALE_EVENT_URL (see
    event_url), even under the CENTRALE_SPAWN_CMD test override; a
    resolved claude/codex agent's argv additionally gets a
    lifecycle-event hook/notify override appended (see
    _inject_agent_hooks) -- skipped, like the rest of agent resolution
    and the Done-status check, when CENTRALE_SPAWN_CMD is set (see
    spawn_cmd).

    Returns {"session": <name>, "attach": "tmux attach -t <name>",
    "agent": <name>}, plus a "warnings" list of human-readable strings if
    the claim/commit step hit a problem (it never blocks the spawn).
    Raises SpawnError on any failure; never raises anything else and
    never touches git/tmux until project + taskId are validated.
    """
    project, name = _validate_and_check_session(config, project_name, task_id)

    # CENTRALE_SPAWN_CMD overrides everything, including assignee-driven
    # agent selection, so we never call out to `backlog task view` (and
    # never launch a real agent) when it's set — this is what keeps the
    # test suite hermetic (tests spawn probes onto arbitrary/fixture task
    # ids that don't necessarily reflect real backlog task status). Real
    # spawns always run the Done-status check below, before any side
    # effect (task-41).
    task = None
    if not spawn_cmd_override():
        task = _check_not_done(project, task_id)

    # task-70: a branch adopted into a foreign worktree can't be checked
    # out a second time -- refuse with the path, before the claim below
    # commits anything on the base branch.
    _check_not_checked_out_externally(config, project, task_id)

    # Claim the task and commit that claim on the base branch *before*
    # cutting the worktree, so the worktree's branch point already
    # contains it (see _claim_and_commit) — this runs even under the
    # CENTRALE_SPAWN_CMD override, which only replaces the launched
    # command, not this workflow step.
    warnings = _claim_and_commit(project, task_id)

    wt_dir = _ensure_worktree(config, project, task_id)

    if spawn_cmd_override():
        cmd = spawn_cmd()
        agent_name = None
        agent_kind = None
        prompt_suffix = None
    else:
        agent_name, cmd, prompt_suffix = resolve_agent(config, project, task_id, task=task)
        agent_kind = agent_kind_for_command(cmd)

    prompt = prompt_for(task_id, config)
    if prompt_suffix:
        prompt = f"{prompt}\n\n{prompt_suffix}"
    tmux_args = new_session_args(name, wt_dir)
    if agent_name is not None:
        tmux_args += ["-e", f"CENTRALE_AGENT={agent_name}"]
    tmux_args += [
        "-e",
        f"CENTRALE_EVENT_URL={event_url(config, project_name, task_id, agent_kind=agent_kind)}",
    ]
    extra_argv = _inject_agent_hooks(cmd) if agent_name is not None else []
    tmux_args += [*cmd, *extra_argv, prompt]

    # working/waiting describe a particular session, but the in-memory
    # event store is keyed by task. Drop the previous session's residue at
    # the last possible moment before launch. Clearing after run_tmux would
    # race the detached agent's first hook event and could erase fresh state.
    server.clear_agent_event(project_name, task_id)
    proc = server.run_tmux(tmux_args)
    if proc.returncode != 0:
        stderr = (proc.stderr or proc.stdout or "tmux new-session failed").strip()
        raise SpawnError(f"tmux new-session failed: {stderr}", status=500)
    hold_session_geometry(name)

    result = {
        "session": name,
        "attach": f"tmux attach -t {name}",
        "agent": agent_name if agent_name is not None else "custom",
    }
    if warnings:
        result["warnings"] = warnings
    return result


RESUME_FALLBACK_NOTE = (
    "\n\nNote: this task's branch already has prior work on it, including "
    "uncommitted changes left behind in this worktree by an earlier, "
    "interrupted session. Review what's already there (git status, git "
    "log, git diff) before continuing, rather than starting over."
)
# Appended to prompt_for(task_id)'s own output (see resume()'s fresh-start
# fallback below) -- PROMPT_TEMPLATE's own no-self-merge boundary is
# already in there, so this note doesn't repeat it separately.


def resume_cmd_for_agent(config, agent_name):
    """The configured `resumeCmd` for `agent_name`, or None if it has
    none. Looked up separately from resolve_agent (rather than folded
    into that function's already-tested 3-value return) since only
    resume() needs it."""
    if not agent_name:
        return None
    agents = config.get("agents") or server.normalize_agents_map(server.DEFAULT_AGENTS)
    agents_lc = {str(key).lower(): value for key, value in agents.items()}
    entry = agents_lc.get(agent_name)
    return entry.get("resumeCmd") if entry else None


RECONCILE_PROMPT_TEMPLATE = (
    "Reconcile backlog task {task_id}'s branch with `{base_branch}`. This "
    "branch has fallen behind `{base_branch}` and the dashboard's merge "
    "gate failed on the combination of the two: the failure is likely a "
    "collision with newer work on `{base_branch}` (textual, or semantic -- "
    "e.g. a test landed there that asserts something this branch "
    "legitimately changed, or vice versa), not necessarily a defect in "
    "this branch. {context}Run `backlog instructions overview` first, then "
    "orient before touching anything: read the task's description, plan, "
    "and implementation notes (`backlog task view {task_id} --plain`), and "
    "review what this branch changed (`git log {base_branch}..HEAD` and "
    "`git diff {base_branch}...HEAD`) so you understand its intent before "
    "merging. Then: merge "
    "`{base_branch}` into this branch (`git merge {base_branch}` -- do NOT "
    "rebase); resolve every conflict, textual and semantic, so the merged "
    "result is correct for both sides' intent; run the project check "
    "command ({check_command}) and fix whatever fails until it passes; "
    "commit the merge and every fix on this branch, including any backlog "
    "task updates, and leave the worktree clean. Do NOT merge this branch "
    "into `{base_branch}` or delete it -- the dashboard's gated merge "
    "handles integration and will re-verify the result."
)

# task-133: the one sentence the RESUMED reconcile adds. The conversation
# being continued built this branch, so it already holds the why of every
# change on it (that is exactly what reconciling a semantic conflict
# needs, and why the prompt now rides along with `--continue`/`resume
# --last` instead of replacing the conversation); the one thing it holds
# that is wrong is its memory of the base branch, which the prompt's own
# orient-then-merge instructions correct by sending it to the repository.
RECONCILE_RESUMED_CONTEXT = (
    "You built this branch in this conversation, so you already know what "
    "it intends and why; `{base_branch}` has moved on since you last "
    "looked at it, and what you remember of `{base_branch}` is stale -- "
    "read the repository, not your memory of it. "
)


def reconcile_prompt_for(task_id, base_branch, check_command=None, resumed=False):
    """The task-66 reconcile prompt: the same no-self-merge boundary as
    PROMPT_TEMPLATE, but the job is merging base_branch INTO the task
    branch and making the combination green -- Centrale itself never
    merges or rebases anything on the agent's behalf. check_command is
    the project's configured checkCommand, spelled out so the agent runs
    exactly what the dashboard's gate 5 will run afterward.

    resumed=True (task-133) is the prompt as delivered alongside a
    resumed conversation (`claude --continue <prompt>`, `codex resume
    --last <prompt>`, or a configured resumeCmd): identical text plus
    RECONCILE_RESUMED_CONTEXT, since "you built this branch; the base
    has moved" is the truer framing for an agent that did. The fresh
    variant (resumed=False) is what a fresh start gets, and what the
    orientation instructions were written for."""
    check = f"`{check_command}`" if check_command else "see the project's AGENTS.md/README"
    context = RECONCILE_RESUMED_CONTEXT.format(base_branch=base_branch) if resumed else ""
    return RECONCILE_PROMPT_TEMPLATE.format(
        task_id=task_id, base_branch=base_branch, check_command=check, context=context
    )


def resume(config, project_name, task_id, reconcile=False):
    """Refuses a task whose status is Done with a 409 (see
    _check_not_done), same as spawn(), before the worktree is touched --
    a Done task's worktree may still exist, and resuming an agent onto
    already-finished work is equally wrong as spawning fresh onto it.

    Otherwise resumes an interrupted agent: reuses the task's EXISTING
    worktree and branch via _ensure_worktree (never re-claims or
    re-commits the task, unlike spawn() -- it was already claimed the
    first time it was spawned) and starts a new tmux session there
    running the resolved agent's resume command, in priority order:

    1. that agent's configured `resumeCmd` (see server.normalize_agents_map),
       if it has one -- no prompt argument, since the resumed conversation
       already has its own context (the reconcile variant below is the
       one exception: there the job is new, and the prompt is appended);
    2. else, the family default for an agent Centrale ships (decided by
       its `cmd`'s first argv element, so a wrapper script or an absolute
       path is treated as neither): `["claude", "--continue"]` for
       "claude", `["codex", "resume", "--last"]` for "codex" -- also no
       prompt, since each continues that worktree's own conversation;
    3. else, a fresh start: that agent's own `cmd` with the standard
       workflow prompt, plus RESUME_FALLBACK_NOTE calling out that prior
       work already exists in the worktree.

    Session naming, the existing-session 409 check, tmux-availability and
    task/project validation, and the CENTRALE_SPAWN_CMD test override all
    behave identically to spawn() (see _validate_and_check_session) --
    this is a spawn variant, not a separate engine. The lifecycle-event
    environment/argv injection (CENTRALE_EVENT_URL always; a claude/codex
    hook/notify override on top of whichever resume command above got
    picked) is identical to spawn()'s too -- see _inject_agent_hooks.

    With reconcile=True (task-66, the drawer's "Resume to reconcile" on a
    branch that fell behind its base and then failed the merge/check
    gate), every step above is identical -- validation, the 409 on a
    live session, the Done refusal, worktree reuse, agent resolution,
    CENTRALE_EVENT_URL and hook injection, and the same three tiers --
    except that reconcile_prompt_for()'s prompt (plus the agent's
    promptSuffix) is the trailing argument at EVERY tier (task-133):
    `resumeCmd + [prompt]`, `["claude", "--continue", prompt]` /
    `["codex", "resume", "--last", prompt]` (both CLIs accept a prompt
    alongside resume and deliver it into the continued conversation), or
    the fresh-start `cmd + [prompt + RESUME_FALLBACK_NOTE]`. Reconciling
    is a fully specified job (merge <base> into the branch, resolve
    textual and semantic conflicts, get the check command green, commit
    on the branch, never merge to <base>), but the hard half of it --
    which of two colliding changes is the one this task meant -- is
    resolved from exactly the why that the conversation which built the
    branch holds, so the conversation is continued rather than replaced
    (before task-133 this variant always started a fresh agent on the
    agent's own `cmd`, to avoid the dead conversation's stale view of
    <base>; the prompt already corrects that by sending the agent to the
    repository, and the resumed variant says so in one extra sentence).
    Centrale performs no merge or rebase of its own here: the agent does
    the judgment work in its worktree and the ordinary gates re-verify
    the result afterward. The response adds "reconcile": true.

    Returns {"session", "attach", "agent", "resumed": true}. Raises
    SpawnError on any failure, same as spawn().
    """
    project, name = _validate_and_check_session(config, project_name, task_id)

    # A Done task's worktree may still exist (left over from before it was
    # merged, or a stray re-click) -- resuming an agent onto it is equally
    # wrong as spawning fresh, so this gets the same refusal spawn() does,
    # before the worktree is touched. Same CENTRALE_SPAWN_CMD skip as
    # spawn() (task-41).
    task = None
    if not spawn_cmd_override():
        task = _check_not_done(project, task_id)

    # task-70: same refusal as spawn() -- the branch is checked out in a
    # worktree Centrale doesn't manage, so there is nothing here to
    # resume into and git would refuse the second checkout anyway.
    _check_not_checked_out_externally(config, project, task_id)

    wt_dir = _ensure_worktree(config, project, task_id)

    # task-133: the reconcile prompt comes in two framings -- `resumed`
    # for a continued conversation (tiers 1 and 2 below), the plain one
    # for a fresh start (tier 3, and the CENTRALE_SPAWN_CMD override,
    # which is a fresh process by construction). Built lazily per tier so
    # the framing always matches the command actually launched.
    def reconcile_prompt(resumed):
        return reconcile_prompt_for(
            task_id, current_branch(project["path"]), project.get("checkCommand"),
            resumed=resumed,
        )

    if spawn_cmd_override():
        cmd = spawn_cmd()
        agent_name = None
        agent_kind = None
        prompt_arg = reconcile_prompt(resumed=False) if reconcile else prompt_for(task_id, config)
    else:
        agent_name, agent_cmd, prompt_suffix = resolve_agent(config, project, task_id, task=task)
        agent_kind = agent_kind_for_command(agent_cmd)
        resume_cmd = resume_cmd_for_agent(config, agent_name)
        # Plain Resume passes no prompt where a conversation is continued
        # (it already has its context); reconcile passes the reconcile
        # prompt at the same tiers, as the trailing argument -- the job is
        # new even though the conversation is not (task-133).
        if resume_cmd:
            cmd = resume_cmd
            prompt_arg = reconcile_prompt(resumed=True) if reconcile else None
        elif agent_cmd and agent_cmd[0] == "claude":
            cmd = ["claude", "--continue"]
            prompt_arg = reconcile_prompt(resumed=True) if reconcile else None
        elif agent_cmd and agent_cmd[0] == "codex":
            # task-115: the codex equivalent of `claude --continue`. Both
            # the picker and --last filter by working directory unless
            # --all is passed ("Show all sessions (disables cwd
            # filtering)"), and every task has its own worktree, so "the
            # most recent session here" is this task's own conversation --
            # verified empirically: the picker in two different repos
            # lists two different sets, and --all lists both plus more.
            # The injected -c hook overrides still apply: `codex resume`
            # takes -c the same way the top-level command does.
            cmd = ["codex", "resume", "--last"]
            prompt_arg = reconcile_prompt(resumed=True) if reconcile else None
        else:
            cmd = agent_cmd
            base_prompt = reconcile_prompt(resumed=False) if reconcile else prompt_for(task_id, config)
            prompt_arg = base_prompt + RESUME_FALLBACK_NOTE
        # The agent's promptSuffix rides along wherever a prompt is passed
        # at all -- the fresh-start fallback, and every reconcile tier.
        if prompt_arg is not None and prompt_suffix:
            prompt_arg = f"{prompt_arg}\n\n{prompt_suffix}"

    tmux_args = new_session_args(name, wt_dir)
    if agent_name is not None:
        tmux_args += ["-e", f"CENTRALE_AGENT={agent_name}"]
    tmux_args += [
        "-e",
        f"CENTRALE_EVENT_URL={event_url(config, project_name, task_id, agent_kind=agent_kind)}",
    ]
    extra_argv = _inject_agent_hooks(cmd) if agent_name is not None else []
    tmux_args += list(cmd) + extra_argv
    if prompt_arg is not None:
        tmux_args.append(prompt_arg)

    # A resumed conversation is still a new tmux session. Its predecessor's
    # last lifecycle event must not become the new session's initial badge;
    # clear before launch so the new agent's first hook cannot be lost.
    server.clear_agent_event(project_name, task_id)
    proc = server.run_tmux(tmux_args)
    if proc.returncode != 0:
        stderr = (proc.stderr or proc.stdout or "tmux new-session failed").strip()
        raise SpawnError(f"tmux new-session failed: {stderr}", status=500)
    hold_session_geometry(name)

    result = {
        "session": name,
        "attach": f"tmux attach -t {name}",
        "agent": agent_name if agent_name is not None else "custom",
        "resumed": True,
    }
    if reconcile:
        result["reconcile"] = True
    return result
