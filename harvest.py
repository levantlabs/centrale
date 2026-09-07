"""Gated auto-merge ("harvest") engine for `GET /api/harvest` and
`POST /api/harvest`: merges a finished `task/<id>` branch back into its
project's base branch, but only once five explicit safety gates all pass.

Mirrors spawn.py's pattern: imported lazily by server.py (inside the
request handler) to avoid a circular import, and every subprocess call
goes through the `server`/`spawn` module objects at call time so tests
can patch `server.run_git` / `server.run_backlog` / `server.list_sessions`
/ `server.run_check_command` in one place.

Python 3.12 stdlib only.
"""

from __future__ import annotations

import collections
import os
import re
import shutil
import tempfile
import threading
import time

import server
import spawn

# The five gates evaluated by evaluate_branch(), in order. Evaluation
# stops at the first failure -- later gates are reported as "not
# evaluated" rather than run, since e.g. there's no point dry-running a
# merge for a task that isn't even Done yet.
GATE_NAMES = ["noLiveSession", "taskDone", "worktreeClean", "mergeClean", "checkCommand"]

_BRANCH_TASK_RE = re.compile(r"^task/([a-z]+)-([0-9]+(?:\.[0-9]+)*)$")

# Serializes every real harvest attempt (click or auto) system-wide, so a
# click-triggered harvest and the background auto-harvest thread can
# never interleave their git operations. Evaluation-only calls (GET,
# never acts) don't need it.
_harvest_lock = threading.Lock()

# Bounded in-memory log of recent harvest *attempts* (never evaluate-only
# calls), newest last. Exposed via GET /api/harvest's "events" list so
# auto-harvest outcomes are observable without digging through logs.
_events_lock = threading.Lock()
_events = collections.deque(maxlen=50)

# task-96: the two non-gate stages harvest_branch() reports progress for
# after evaluate_branch() has returned green -- the immediate pre-merge
# re-check of the project's own checkout (reported as a sixth gate on
# the report itself, see _harvest_branch_locked) and the real merge.
STAGE_MAIN_CHECKOUT_CLEAN = "mainCheckoutClean"
STAGE_MERGE = "merge"

# Every stage name current_progress() can report, in the order a
# successful merge passes through them. Nothing depends on the order at
# runtime -- the frontend is told which stage is running and never infers
# one -- but the docs and tests do.
PROGRESS_STAGES = GATE_NAMES + [STAGE_MAIN_CHECKOUT_CLEAN, STAGE_MERGE]

# Live progress of the ONE harvest attempt in flight, or None when
# nothing is being harvested. Deliberately a single record rather than a
# map: _harvest_lock already serializes every real attempt (click, "merge
# all", and the auto-harvest thread) system-wide, so there is never more
# than one to describe. Evaluate-only reads (GET /api/harvest) publish
# nothing at all -- they don't act, and they don't take the lock.
#
# This is live state, never history: it exists only between
# _begin_progress() and the _clear_progress() in harvest_branch()'s
# finally, so a finished, gate-blocked or crashed harvest leaves nothing
# a later read could show. The events log above is the durable record.
_progress_lock = threading.Lock()
_progress = None


def _begin_progress(project_name, task_id, branch, trigger):
    """Publishes "a harvest of this branch has started", with no stage
    yet -- a reader that catches this window sees the attempt but no gate
    name, which the frontend renders as its plain fallback rather than a
    guess."""
    global _progress
    with _progress_lock:
        _progress = {
            "project": project_name,
            "taskId": task_id,
            "branch": branch,
            "trigger": trigger,
            "gate": None,
        }


def _set_progress_stage(name):
    """Records the gate/stage now running. A no-op when nothing is in
    flight, so it is safe as an evaluate_branch() callback wherever one
    is wired up."""
    global _progress
    with _progress_lock:
        if _progress is not None:
            _progress["gate"] = name


def _clear_progress():
    global _progress
    with _progress_lock:
        _progress = None


def current_progress():
    """The in-flight harvest's progress record, or None. A copy, so a
    caller can never hold a reference that keeps mutating (or that
    outlives the attempt) -- see GET /api/harvest-progress."""
    with _progress_lock:
        return dict(_progress) if _progress is not None else None


DEFAULT_AUTO_HARVEST_INTERVAL = 30.0


class HarvestError(Exception):
    """Raised for a harvest *request*-level failure (unknown project,
    malformed task id, missing project path). A *gate* failure is a
    normal, structured result (see evaluate_branch), never an exception
    -- this is only for input Centrale can't act on at all."""

    def __init__(self, message, status=400):
        super().__init__(message)
        self.message = message
        self.status = status

    def __str__(self):
        return self.message


def _record_event(project_name, report, trigger, error=None):
    """Appends a compact summary of one harvest *attempt* (click or
    auto; success or failure) to the bounded in-memory events log."""
    event = {
        "time": time.time(),
        "project": project_name,
        "taskId": report.get("taskId"),
        "branch": report.get("branch"),
        "trigger": trigger,
        "merged": bool(report.get("merged")),
    }
    if error:
        event["error"] = error
    elif report.get("merged"):
        event["baseBranch"] = report.get("baseBranch")
    elif report.get("alreadyMerged"):
        event["alreadyMerged"] = True
        event["reason"] = "already merged (branch no longer exists)"
    else:
        failed = next((g for g in (report.get("gates") or []) if g.get("passed") is False), None)
        if failed:
            event["reason"] = f"{failed['name']}: {failed.get('reason')}"
    with _events_lock:
        _events.append(event)


def recent_events(project_name=None):
    """Recent harvest-attempt events, newest last. Filtered to one
    project when given, since GET /api/harvest is itself project-scoped."""
    with _events_lock:
        events = list(_events)
    if project_name is not None:
        events = [e for e in events if e.get("project") == project_name]
    return events


def _reset_events():
    """Test helper: clears the in-memory events log."""
    with _events_lock:
        _events.clear()


def _find_project(config, project_name):
    for project in config.get("projects", []):
        if project.get("name") == project_name:
            return project
    return None


def task_id_from_branch(branch):
    """Recovers the canonical task ID (e.g. "TASK-2") from a
    task/<id-lower> branch name (e.g. "task/task-2"), or None if the
    branch doesn't match that shape. Public: server.py's board aggregation
    also uses this (with list_task_branches) to compute each task's
    "hasSpawnBranch" flag."""
    m = _BRANCH_TASK_RE.match(branch)
    if not m:
        return None
    return f"{m.group(1).upper()}-{m.group(2)}"


def list_task_branches(repo_path):
    """All local branches matching task/<id>, e.g. ["task/task-2", ...].
    Never raises; an unreadable repo just yields no branches."""
    proc = server.run_git(
        ["for-each-ref", "--format=%(refname:short)", "refs/heads/task"], cwd=repo_path
    )
    if proc.returncode != 0:
        return []
    return [line.strip() for line in (proc.stdout or "").splitlines() if line.strip()]


def _branch_exists(repo_path, branch):
    """Cheap single-ref existence check, used by evaluate_branch() to
    tell "nothing here to evaluate, it's already merged/gone" apart from
    a genuine gate failure -- mirrors spawn.py's private helper of the
    same name/shape, kept separate rather than shared since both are
    module-internal and cross-module calls in this codebase only ever
    go through public functions."""
    proc = server.run_git(["rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"], cwd=repo_path)
    return proc.returncode == 0


def _gate(name, passed, reason=None):
    return {"name": name, "passed": passed, "reason": reason}


def _skipped(name):
    return {"name": name, "passed": None, "reason": "not evaluated (an earlier gate failed)"}


def _gate_no_live_session(project_name, task_id):
    try:
        sessions = server.list_sessions()
    except server.BacklogError as exc:
        return False, f"failed to check live sessions: {exc}"
    name = spawn.session_name(project_name, task_id)
    if name in {s.get("name") for s in sessions}:
        return False, f"a live tmux session is still running: {name}"
    return True, None


def _gate_task_done(wt_dir, task_id):
    """Returns (passed, reason, task_dict_or_None). Reads the task's
    committed state from its own worktree (cwd=wt_dir), not the project's
    main checkout: an agent's Done status, checked ACs, and implementation
    notes are committed only on its task/<id> branch until that branch is
    actually merged, so reading from main here would fail every properly-
    finished spawn's gate."""
    try:
        data = server.run_backlog(["task", "view", task_id, "--json"], cwd=wt_dir)
    except server.BacklogError as exc:
        return False, f"failed to read task {task_id}: {exc}", None

    task = data.get("task") if isinstance(data, dict) else None
    if not isinstance(task, dict):
        return False, "unexpected task-view response", None

    status = task.get("status")
    if status != "Done":
        return False, f"task status is {status!r}, not Done", task

    ac = task.get("acceptanceCriteria") or []
    unchecked = [item.get("index") for item in ac if not item.get("checked")]
    if unchecked:
        indices = ", ".join(f"#{i}" for i in unchecked)
        return False, f"AC {indices} unchecked", task

    return True, None, task


def _gate_task_done_from_branch(project, branch, task_id):
    """Read a parked branch's committed task through the Backlog CLI.

    The detached snapshot (spawn.detached_snapshot -- shared with GET
    /api/task's "branchTask" for a parked branch, so both read the same
    tree the same way) exposes the exact branch tree without checking
    out, and therefore reserving, ``branch`` itself.
    """
    with spawn.detached_snapshot(project["path"], branch) as (tmp_dir, error):
        if error is not None:
            return (
                False,
                f"failed to read task {task_id} from {branch}: "
                f"could not create a detached snapshot: {error}",
                None,
            )
        return _gate_task_done(tmp_dir, task_id)


def _task_from_backlog(cwd, task_id):
    """Best-effort task-view read used only for divergence context.

    Gate 2 deliberately keeps its own stricter read above: failure there
    fails the gate. A main-checkout read here is presentation/action
    metadata layered on top of that branch-side decision, so failure just
    means there is no Done divergence to offer.
    """
    try:
        data = server.run_backlog(["task", "view", task_id, "--json"], cwd=cwd)
    except server.BacklogError:
        return None
    task = data.get("task") if isinstance(data, dict) else None
    return task if isinstance(task, dict) else None


def _is_done_status(status):
    return str(status or "").strip().lower() == "done"


def _safe_repo_relative_path(path):
    """Return path when it is safe to pass as a repo-relative pathspec."""
    if not isinstance(path, str) or not path or os.path.isabs(path):
        return None
    normalized = os.path.normpath(path)
    if normalized == ".." or normalized.startswith(".." + os.sep):
        return None
    return normalized


def _safe_task_path(path, task_id):
    """Return only the canonical Backlog task path for this exact task."""
    normalized = _safe_repo_relative_path(path)
    expected_prefix = f"backlog/tasks/{task_id.lower()} - "
    if not normalized or not normalized.startswith(expected_prefix):
        return None
    return normalized


def _last_branch_commit_subject(repo_path, branch):
    proc = server.run_git(["log", "-1", "--format=%s", branch], cwd=repo_path)
    if proc.returncode != 0:
        return None
    subject = (proc.stdout or "").strip()
    return subject or None


def _done_divergence(project, branch, task_id, branch_task):
    """Describe main saying Done while the agent branch does not.

    This never changes gate truth: gate 2 has already read and rejected the
    branch task. The extra main-side read only explains the visible board
    contradiction and supplies the explicit adopt action with fresh context.
    """
    branch_status = branch_task.get("status")
    if _is_done_status(branch_status):
        return None

    main_task = _task_from_backlog(project["path"], task_id)
    if not main_task or not _is_done_status(main_task.get("status")):
        return None

    branch_task_path = _safe_task_path(branch_task.get("path"), task_id)
    main_task_path = _safe_task_path(main_task.get("path"), task_id)
    main_task_uncommitted = False
    if main_task_path:
        status_proc = server.run_git(
            ["status", "--porcelain", "--", main_task_path], cwd=project["path"]
        )
        if status_proc.returncode == 0:
            staged, other = _parse_dirty_paths(status_proc.stdout or "")
            main_task_uncommitted = main_task_path in staged or main_task_path in other

    return {
        "branchStatus": branch_status,
        "boardStatus": main_task.get("status"),
        "branchTaskPath": branch_task_path,
        "mainTaskPath": main_task_path,
        "mainTaskUncommitted": main_task_uncommitted,
        "lastBranchCommitSubject": _last_branch_commit_subject(project["path"], branch),
    }


def _missing_worktree_reason(branch, checkout):
    """The gate reason for a Centrale worktree that isn't on disk,
    honest about WHY (task-70): a branch adopted into a foreign worktree
    names that checkout -- work may well be continuing there, and the
    old "was it removed manually?" guess was actively misleading for it.
    That guess is kept only for a branch checked out nowhere (parked --
    the worktree was genuinely removed) or one git still lists at the
    Centrale path whose directory is gone (rm -rf'd without `git
    worktree remove`)."""
    kind = checkout.get("kind")
    if kind == "external":
        return (
            f"worktree not found -- {branch} is checked out outside Centrale at "
            f"{checkout.get('path')} (worked externally); merge once that "
            f"worktree is finished and removed"
        )
    if kind == "centrale":
        return (
            f"worktree not found (was it removed manually?) -- git still lists "
            f"{branch} at {checkout.get('path')}"
        )
    return f"worktree not found (was it removed manually?) -- {branch} is checked out nowhere (parked branch)"


def _checkout_state_for_merge(config, project, task_id):
    """Return a trustworthy checkout classification for a merge gate.

    ``spawn.checkout_state`` intentionally treats an unreadable worktree
    list as empty because its board use is only a display hint. A parked
    merge would turn that empty map into a safety decision, so harvest must
    fail closed when git cannot enumerate every checkout.
    """
    proc = server.run_git(["worktree", "list", "--porcelain"], cwd=project["path"])
    if proc.returncode != 0:
        stderr = (proc.stderr or proc.stdout or "unknown error").strip()
        return None, f"failed to list git worktrees: {stderr}"
    checkouts = spawn.parse_worktree_checkouts(proc.stdout or "")
    return spawn.checkout_state(config, project, task_id, checkouts=checkouts), None


def _gate_worktree_clean(config, project, task_id, branch):
    """Returns (passed, reason, wt_dir_or_None, base_branch_or_None).
    wt_dir/base_branch are only meaningful when the gate passed -- later
    gates need them, nothing does otherwise."""
    repo_path = project["path"]
    wt_dir = spawn.worktree_dir(config, project["name"], task_id)

    clean_reason = None
    clean_wt_dir = wt_dir
    if os.path.isdir(wt_dir):
        status_proc = server.run_git(["status", "--porcelain"], cwd=wt_dir)
        if status_proc.returncode != 0:
            stderr = (status_proc.stderr or status_proc.stdout or "unknown error").strip()
            return False, f"failed to check worktree status: {stderr}", None, None
        if (status_proc.stdout or "").strip():
            return False, "worktree has uncommitted changes", None, None
    else:
        checkout, checkout_error = _checkout_state_for_merge(config, project, task_id)
        if checkout_error:
            return False, checkout_error, None, None
        if checkout["kind"] != "none":
            return False, _missing_worktree_reason(branch, checkout), None, None
        clean_reason = "no worktree -- nothing uncommitted to protect"
        clean_wt_dir = None

    base_branch = spawn.current_branch(repo_path)
    count_proc = server.run_git(["rev-list", "--count", f"{base_branch}..{branch}"], cwd=repo_path)
    if count_proc.returncode != 0:
        stderr = (count_proc.stderr or count_proc.stdout or "unknown error").strip()
        return False, f"failed to compare {branch} to {base_branch}: {stderr}", None, None
    try:
        commit_count = int((count_proc.stdout or "0").strip())
    except ValueError:
        commit_count = 0
    if commit_count == 0:
        return False, f"nothing to merge — no commits beyond {base_branch}", None, None

    return True, clean_reason, clean_wt_dir, base_branch


def _parse_dirty_paths(porcelain_output):
    """Parses `git status --porcelain` output into (staged_paths,
    other_paths). staged_paths -- where the index already differs from
    HEAD (the first, "X", column is non-blank and non-'?') -- are an
    unconditional merge blocker regardless of whether the incoming
    branch touches them at all: empirically verified (see
    tests_integration/test_harvest_integration.py), `git merge --no-ff`
    refuses on ANY staged change, even to a path the branch's own diff
    never mentions, because it can't safely construct a merge commit
    while the index disagrees with HEAD. other_paths covers both
    unstaged modifications to already-tracked files and untracked files
    ("??") -- these are only a problem if they overlap with what the
    merge would actually touch (also empirically verified: an untracked
    file at a path the branch would create makes git refuse it as if it
    would be overwritten by the merge, the same as an unstaged
    modification to a path the branch also modifies -- but an unrelated
    untracked or unstaged-modified file merges past cleanly).

    Every path is run through server.dequote_git_path first -- git
    quotes (and backslash/octal-escapes) a path the instant it contains
    a space or other special character, which every Backlog.md task
    filename always does, and comparing that quoted form against another
    command's unquoted one (see _merge_touched_paths) silently never
    matches (see server.dequote_git_path's docstring for the full
    story)."""
    staged = set()
    other = set()
    for line in porcelain_output.splitlines():
        if not line or len(line) < 4:
            continue
        index_status, worktree_status, path = line[0], line[1], line[3:]
        if " -> " in path:
            old_path, _, new_path = path.partition(" -> ")
            paths = [server.dequote_git_path(old_path), server.dequote_git_path(new_path)]
        else:
            paths = [server.dequote_git_path(path)]
        if index_status == "?" and worktree_status == "?":
            other.update(paths)
            continue
        if index_status not in (" ", "?"):
            staged.update(paths)
        if worktree_status not in (" ", "?"):
            other.update(paths)
    return staged, other


def _unstaged_tracked_paths(porcelain_output):
    """Tracked paths changed only in the working tree, never the index.

    These are the only paths the board-side discard affordance may restore.
    Staged, untracked, renamed, or mixed index/worktree changes keep the
    existing mainCheckoutClean refusal and are never touched automatically.
    """
    paths = set()
    for line in porcelain_output.splitlines():
        if not line or len(line) < 4:
            continue
        index_status, worktree_status, raw_path = line[0], line[1], line[3:]
        if index_status != " " or worktree_status != "M":
            continue
        if " -> " in raw_path:
            continue
        paths.add(server.dequote_git_path(raw_path))
    return paths


def _merge_touched_paths(repo_path, base_branch, branch):
    """The set of paths `git merge branch` (from base_branch) would
    touch, via `git diff --name-only`. Returns None -- distinct from an
    empty set, which means "a real, empty diff" -- on a git failure, so
    a caller unable to determine this refuses conservatively rather than
    treating "couldn't check" as "nothing overlaps". Dequoted through
    server.dequote_git_path -- see _parse_dirty_paths' docstring."""
    proc = server.run_git(["diff", "--name-only", f"{base_branch}..{branch}"], cwd=repo_path)
    if proc.returncode != 0:
        return None
    return {
        server.dequote_git_path(line.strip())
        for line in (proc.stdout or "").splitlines() if line.strip()
    }


def _behind_base_count(repo_path, base_branch, branch):
    """How many commits base_branch has that `branch` does not -- i.e.
    commits on the base not reachable from the branch tip, counted from
    their merge-base (`git rev-list --count <branch>..<base>`). 0 means
    the branch already contains everything on its base. Returns None on
    any git failure (a caller treats "couldn't tell" as "no hint",
    never as "behind"): this is a display hint feeding the drawer's
    resume-to-reconcile offer (task-66), not a safety gate, so it fails
    quiet rather than loud. Derived fresh from git on every evaluation;
    nothing is stored."""
    proc = server.run_git(["rev-list", "--count", f"{branch}..{base_branch}"], cwd=repo_path)
    if proc.returncode != 0:
        return None
    try:
        return int((proc.stdout or "").strip() or "0")
    except ValueError:
        return None


def _reconcile_hint(base_branch, count):
    plural = "" if count == 1 else "s"
    return (
        f"this branch predates {count} newer commit{plural} on {base_branch}; "
        "the failure may be a collision with newer work, not a defect in the branch"
    )


def _annotate_behind_base(report, repo_path, base_branch, branch):
    """task-66: on a report whose merge/check gate failed, attach how far
    the branch has fallen behind its base plus the collision hint the
    drawer shows next to the gate reason -- ONLY when it actually is
    behind. An up-to-date branch (count 0) and an undeterminable one
    (git failure) get no fields at all, so "no behindBase key" is the
    single "no hint, no reconcile offer" signal for the frontend."""
    count = _behind_base_count(repo_path, base_branch, branch)
    if count:
        report["behindBase"] = {"baseBranch": base_branch, "count": count}
        report["reconcileHint"] = _reconcile_hint(base_branch, count)
    return report


def _scratch_merge_and_check(project, branch, base_branch, check_command, on_check=None):
    """Gates 4+5 share one throwaway, detached temporary worktree cut
    from base_branch: attempt `git merge branch` there (gate 4 -- never
    touches the project's own checkout), and if that's clean, optionally
    run check_command in that same merged worktree (gate 5). The temp
    worktree is always removed before returning, on every path.

    Returns (merge_ok, merge_reason, check_ok_or_None, check_reason).
    check_ok is None when there's no check_command to run (gate 5
    passes vacuously) or when the merge itself failed (gate 5 was never
    reached).

    `on_check` (task-96) is called once, with no arguments, at the single
    moment gate 5 actually starts running check_command -- after the
    scratch merge succeeded and only when there is a command to run. It
    has to be announced from in here rather than by the caller: a project
    with no checkCommand passes gate 5 vacuously without anything running,
    and saying "running tests" for that would be a guess, not an
    observation.
    """
    repo_path = project["path"]
    tmp_dir = tempfile.mkdtemp(prefix="centrale-harvest-")
    # `git worktree add` wants to create the directory itself.
    os.rmdir(tmp_dir)
    try:
        add_proc = server.run_git(["worktree", "add", "--detach", tmp_dir, base_branch], cwd=repo_path)
        if add_proc.returncode != 0:
            stderr = (add_proc.stderr or add_proc.stdout or "unknown error").strip()
            return False, f"failed to create a scratch worktree: {stderr}", None, None

        merge_proc = server.run_git(["merge", "--no-ff", "--no-edit", branch], cwd=tmp_dir)
        if merge_proc.returncode != 0:
            stderr = (merge_proc.stderr or merge_proc.stdout or "merge failed").strip()
            server.run_git(["merge", "--abort"], cwd=tmp_dir)
            return False, f"merge conflict: {stderr}", None, None

        if not check_command:
            return True, None, True, None

        if on_check is not None:
            on_check()

        # A project's own checkTimeoutSeconds override (load_config
        # already defaults it to server.DEFAULT_CHECK_TIMEOUT_SECONDS for
        # every project, but a project dict built by hand -- as some
        # tests do -- may not have the key at all).
        check_timeout = project.get("checkTimeoutSeconds") or server.DEFAULT_CHECK_TIMEOUT_SECONDS
        check_proc = server.run_check_command(check_command, cwd=tmp_dir, timeout=check_timeout)
        if check_proc.returncode != 0:
            tail = ((check_proc.stdout or "") + "\n" + (check_proc.stderr or "")).strip()
            if check_proc.returncode == 124:
                # _run() synthesizes this returncode specifically for a
                # subprocess.TimeoutExpired -- a hung checkCommand is a
                # failed gate with a clear reason, never a hung request.
                return True, None, False, f"checkCommand timed out after {check_timeout}s"
            return True, None, False, f"checkCommand failed: {tail[-500:] or 'no output'}"
        return True, None, True, None
    finally:
        server.run_git(["worktree", "remove", "--force", tmp_dir], cwd=repo_path)
        shutil.rmtree(tmp_dir, ignore_errors=True)


def evaluate_branch(config, project, task_id, on_gate=None):
    """Evaluates all five harvest gates for task/<id> in `project`,
    stopping at the first failure. Never raises, and never leaves
    anything behind in the project's own repo: a parked branch's gate-2
    task read and gates 4/5 do their work in throwaway detached worktrees
    that are always removed before this returns, regardless of outcome.

    Returns {"taskId", "branch", "harvestable", "gates": [...],
    "taskTitle": str | None, "taskPath": str | None} -- plus, when the
    mergeClean or checkCommand gate failed AND the branch is behind its
    base, "behindBase": {"baseBranch", "count"} and "reconcileHint"
    (task-66, see _annotate_behind_base) -- or -- if the branch itself no longer exists
    (already merged by this call, a concurrent one, or removed by hand)
    -- {"taskId", "branch", "harvestable": False, "gates": [],
    "taskTitle": None, "taskPath": None, "alreadyMerged": True}: there's
    nothing here to evaluate at all, which is a different situation from
    any gate failing, and reported as such rather than as a confusing
    "worktree not found" gate-2 message. With no Centrale worktree, the
    report also carries "branchCheckout" (task-70, see
    spawn.checkout_state): a foreign or stale Centrale checkout still
    refuses, while a branch checked out nowhere is read from its committed
    tree and may proceed through the remaining gates.

    `on_gate`, when given, is called with each gate's name immediately
    before that gate runs -- the observation hook harvest_branch() wires
    to the live progress record (task-96) so a click can be told which
    gate it is waiting on. It is never called for a gate that is skipped
    rather than run, nor for the already-merged early return. Default
    None, so an evaluate-only GET publishes nothing.
    """
    branch = spawn.branch_name(task_id)

    def began(name):
        if on_gate is not None:
            on_gate(name)

    if not _branch_exists(project["path"], branch):
        return {
            "taskId": task_id,
            "branch": branch,
            "harvestable": False,
            "gates": [],
            "taskTitle": None,
            "taskPath": None,
            "alreadyMerged": True,
        }

    gates = []
    task_title = None
    task_path = None
    branch_checkout = None

    began(GATE_NAMES[0])
    ok, reason = _gate_no_live_session(project["name"], task_id)
    gates.append(_gate(GATE_NAMES[0], ok, reason))
    if not ok:
        gates += [_skipped(n) for n in GATE_NAMES[1:]]
        return _report(task_id, branch, gates, task_title, task_path)

    # Gate 2 always reads the branch's task state. A managed worktree is
    # the ordinary source; a parked branch gets a detached, temporary
    # snapshot so the Backlog CLI still performs the task lookup. Any
    # checkout elsewhere remains a refusal because work may be continuing.
    began(GATE_NAMES[1])
    wt_dir = spawn.worktree_dir(config, project["name"], task_id)
    if os.path.isdir(wt_dir):
        ok, reason, task = _gate_task_done(wt_dir, task_id)
    else:
        branch_checkout, checkout_error = _checkout_state_for_merge(
            config, project, task_id
        )
        if checkout_error:
            ok, reason, task = False, checkout_error, None
        elif branch_checkout["kind"] == "none":
            ok, reason, task = _gate_task_done_from_branch(project, branch, task_id)
        else:
            ok = False
            reason = _missing_worktree_reason(branch, branch_checkout)
            task = None

    if task:
        task_title = task.get("title")
        task_path = _safe_task_path(task.get("path"), task_id)
    divergence = _done_divergence(project, branch, task_id, task) if task and not ok else None
    if divergence:
        location = "board/main checkout"
        if divergence.get("mainTaskUncommitted") and divergence.get("mainTaskPath"):
            location += f" via an uncommitted edit to {divergence['mainTaskPath']}"
        reason = (
            f"agent branch status is {divergence.get('branchStatus')!r}; "
            f"{location} status is {divergence.get('boardStatus')!r}"
        )
    gates.append(_gate(GATE_NAMES[1], ok, reason))
    if not ok:
        gates += [_skipped(n) for n in GATE_NAMES[2:]]
        report = _report(
            task_id, branch, gates, task_title, task_path, branch_checkout
        )
        if divergence:
            report["doneDivergence"] = divergence
        return report

    began(GATE_NAMES[2])
    ok, reason, wt_dir, base_branch = _gate_worktree_clean(config, project, task_id, branch)
    gates.append(_gate(GATE_NAMES[2], ok, reason))
    if not ok:
        gates += [_skipped(n) for n in GATE_NAMES[3:]]
        return _report(
            task_id, branch, gates, task_title, task_path, branch_checkout
        )

    began(GATE_NAMES[3])
    check_command = project.get("checkCommand")
    merge_ok, merge_reason, check_ok, check_reason = _scratch_merge_and_check(
        project, branch, base_branch, check_command, on_check=lambda: began(GATE_NAMES[4])
    )
    gates.append(_gate(GATE_NAMES[3], merge_ok, merge_reason))
    if not merge_ok:
        gates.append(_skipped(GATE_NAMES[4]))
        report = _report(
            task_id, branch, gates, task_title, task_path, branch_checkout
        )
        return _annotate_behind_base(report, project["path"], base_branch, branch)

    gates.append(_gate(GATE_NAMES[4], check_ok, check_reason))
    report = _report(
        task_id, branch, gates, task_title, task_path, branch_checkout
    )
    if not check_ok:
        # Gates 4 and 5 are the only two whose outcome depends on what's
        # on the base branch (they run against the scratch-merged
        # combination), so they're the only failures a behind-main
        # branch can explain -- a stale branch that isn't Done yet, or
        # has a dirty worktree, is simply not finished. See
        # _annotate_behind_base.
        return _annotate_behind_base(report, project["path"], base_branch, branch)
    return report


def _report(task_id, branch, gates, task_title, task_path, branch_checkout=None):
    harvestable = all(g["passed"] for g in gates)
    report = {
        "taskId": task_id,
        "branch": branch,
        "harvestable": harvestable,
        "gates": gates,
        "taskTitle": task_title,
        "taskPath": task_path,
    }
    if branch_checkout is not None:
        report["branchCheckout"] = branch_checkout
    return report


def evaluate_all_branches(config, project_name):
    """Evaluates every task/<id> branch in a project's gate status
    without acting on any of them (GET /api/harvest). Raises
    HarvestError for an unknown project or an unreadable repo path."""
    project = _find_project(config, project_name)
    if project is None:
        raise HarvestError(f"unknown project: {project_name}", status=404)
    repo_path = project.get("path") or ""
    if not os.path.isdir(repo_path):
        raise HarvestError(f"project path not found: {repo_path or '(empty)'}", status=502)

    reports = []
    for branch in list_task_branches(repo_path):
        task_id = task_id_from_branch(branch)
        if task_id is None:
            continue
        reports.append(evaluate_branch(config, project, task_id))
    return reports


def harvest_branch(
    config,
    project_name,
    task_id,
    trigger="click",
    *,
    adopt_done=False,
    discard_main_task_edit=False,
):
    """Evaluates all five gates for task/<id> and, only if every one
    passes plus one final immediate pre-merge check (the project's own
    checkout must have no uncommitted change that would actually
    conflict with this merge -- gates 4/5 never touch it, so this is
    re-checked right before the real merge rather than assumed; see
    _parse_dirty_paths/_merge_touched_paths for the overlap rule --
    unrelated dirt elsewhere in the checkout never blocks a merge, only
    a staged change anywhere or an unstaged/untracked change at a path
    the merge itself would touch does, matching git's own real
    tolerance rather than refusing on any uncommitted change at all),
    performs the actual merge into the project's base branch, then
    removes a managed worktree when one exists and deletes the branch.

    Serializes with every other harvest attempt (click or the
    background auto-harvest thread) system-wide via a single lock, so
    two harvests can never interleave their git operations -- this is
    also the *only* place that ever merges, so a click and an
    auto-harvest cycle both go through this exact code path with
    identical ordering guarantees, never separate merge logic.

    Every attempt (successful, gate-blocked, or an unexpected merge
    failure) is recorded to the in-memory events log (see
    recent_events()), tagged with `trigger` ("click" or "auto") so the
    frontend can tell which one to surface as a toast.

    While the attempt runs it also publishes the stage it is currently on
    to the live progress record (task-96, see current_progress()) --
    named as it is entered, never predicted -- so a click can be told
    that it is waiting on, say, the checkCommand gate rather than just
    "merging". Because this is the single code path every real merge
    takes, "merge all" and the auto-harvest thread report through exactly
    the same record; there is no second mechanism for them. The record is
    always cleared before this returns or raises.

    Cleanup failures (worktree/branch) don't undo an already-successful
    merge; they're surfaced as a "warnings" list instead, same
    convention as spawn.spawn().

    Returns the same shape as evaluate_branch() plus "merged": bool, and
    on a successful merge "baseBranch", optionally "warnings", and
    optionally "unrelatedDirtyCount" (the number of uncommitted files
    left untouched in the checkout because they didn't overlap with this
    merge -- omitted, not zero, when there were none).

    Explicit action flags are the informed-confirm affordances for a Done
    divergence. Each re-validates its preconditions while holding the same
    lock, performs one confined mutation, then calls the ordinary gate/merge
    path again. Invalid action preconditions and subprocess failures raise
    HarvestError without forcing, stashing, or deleting recoverable state.
    """
    if not isinstance(project_name, str) or not project_name:
        raise HarvestError("missing or invalid project", status=400)
    if not isinstance(task_id, str) or not task_id:
        raise HarvestError("missing or invalid taskId", status=400)
    if not isinstance(adopt_done, bool) or not isinstance(discard_main_task_edit, bool):
        raise HarvestError("harvest action flags must be booleans", status=400)
    if adopt_done and discard_main_task_edit:
        raise HarvestError("choose only one harvest action at a time", status=400)

    project = _find_project(config, project_name)
    if project is None:
        raise HarvestError(f"unknown project: {project_name}", status=404)
    if not server.TASK_ID_RE.match(task_id):
        raise HarvestError(f"invalid task id: {task_id}", status=400)

    # task-121: _harvest_lock alone serializes merges against each
    # other, but not against the OTHER lifecycle routes -- a discard can
    # remove the worktree and delete the branch a merge is running its
    # gates against. The per-task lifecycle lock is taken inside it, and
    # always in that order (global first, then per-task): nothing that
    # holds the per-task lock ever asks for _harvest_lock, so there is
    # no cycle to deadlock on.
    with _harvest_lock, server.task_lifecycle_lock(project_name, task_id):
        _begin_progress(project_name, task_id, spawn.branch_name(task_id), trigger)
        try:
            if adopt_done:
                report = _adopt_done_and_harvest_locked(
                    config, project, project_name, task_id
                )
            elif discard_main_task_edit:
                report = _discard_main_task_edit_and_harvest_locked(
                    config, project, project_name, task_id
                )
            else:
                report = _harvest_branch_locked(config, project, project_name, task_id)
        except HarvestError as exc:
            _record_event(
                project_name,
                {"taskId": task_id, "branch": spawn.branch_name(task_id), "merged": False, "gates": []},
                trigger, error=str(exc),
            )
            raise
        finally:
            # task-96: success, gate failure, HarvestError and any
            # unexpected exception all land here, so nothing about a
            # finished attempt is ever readable afterwards. The events
            # log above is the only thing that outlives the attempt.
            _clear_progress()
    _record_event(project_name, report, trigger)
    return report


def _proc_error(proc):
    return (proc.stderr or proc.stdout or "unknown error").strip()


def _adopt_done_and_harvest_locked(config, project, project_name, task_id):
    """Adopt a freshly re-verified board Done onto the clean task branch."""
    initial = evaluate_branch(config, project, task_id, on_gate=_set_progress_stage)
    divergence = initial.get("doneDivergence")
    if not divergence:
        raise HarvestError(
            "the board/main checkout no longer has a Done status that diverges from the agent branch",
            status=409,
        )

    task_path = _safe_task_path(divergence.get("branchTaskPath"), task_id)
    if not task_path:
        raise HarvestError("cannot safely identify the branch task file to adopt", status=409)

    wt_dir = spawn.worktree_dir(config, project_name, task_id)
    status_proc = server.run_git(["status", "--porcelain"], cwd=wt_dir)
    if status_proc.returncode != 0:
        raise HarvestError(
            f"failed to check the agent worktree before adopting Done: {_proc_error(status_proc)}",
            status=500,
        )
    if (status_proc.stdout or "").strip():
        raise HarvestError(
            "the agent worktree has uncommitted changes; refusing to adopt Done over recoverable work",
            status=409,
        )

    edit_proc = server.run_backlog_raw(
        ["task", "edit", task_id, "-s", "Done"], cwd=wt_dir
    )
    if edit_proc.returncode != 0:
        raise HarvestError(
            f"failed to set {task_id} Done in the agent worktree: {_proc_error(edit_proc)}",
            status=500,
        )

    changed_proc = server.run_git(["status", "--porcelain"], cwd=wt_dir)
    if changed_proc.returncode != 0:
        raise HarvestError(
            f"Done was adopted but checking {task_path} failed: {_proc_error(changed_proc)}; "
            "the worktree edit was left recoverable",
            status=500,
        )
    staged, other = _parse_dirty_paths(changed_proc.stdout or "")
    changed_paths = staged | other
    if changed_paths != {task_path}:
        raise HarvestError(
            "Done was adopted but the Backlog edit did not leave exactly its task file changed; "
            "the worktree changes were left recoverable",
            status=500,
        )

    add_proc = server.run_git(["add", "--", task_path], cwd=wt_dir)
    if add_proc.returncode != 0:
        raise HarvestError(
            f"Done was adopted but staging {task_path} failed: {_proc_error(add_proc)}; "
            "the worktree edit was left recoverable",
            status=500,
        )
    commit_proc = server.run_git(
        [
            "commit",
            "--only",
            "-m",
            f"backlog: adopt board Done for {task_id}",
            "--",
            task_path,
        ],
        cwd=wt_dir,
    )
    if commit_proc.returncode != 0:
        raise HarvestError(
            f"Done was adopted but the branch commit failed: {_proc_error(commit_proc)}; "
            "the staged task edit was left recoverable",
            status=500,
        )

    report = _harvest_branch_locked(config, project, project_name, task_id)
    report["adoptedDone"] = True
    return report


def _discard_main_task_edit_and_harvest_locked(config, project, project_name, task_id):
    """Discard only a freshly re-verified, eligible main task-file edit."""
    initial = _harvest_branch_locked(config, project, project_name, task_id)
    if initial.get("merged"):
        return initial

    discardable = initial.get("discardableMainTaskEdit")
    task_path = _safe_task_path(
        discardable.get("path") if isinstance(discardable, dict) else None, task_id
    )
    if not task_path:
        return initial

    restore_proc = server.run_git(
        ["restore", "--worktree", "--", task_path], cwd=project["path"]
    )
    if restore_proc.returncode != 0:
        raise HarvestError(
            f"failed to discard the board-side edit to {task_path}: {_proc_error(restore_proc)}",
            status=500,
        )

    report = _harvest_branch_locked(config, project, project_name, task_id)
    report["discardedPaths"] = [task_path]
    return report


def _harvest_branch_locked(config, project, project_name, task_id):
    """The actual evaluate-then-merge body of harvest_branch(), run
    under _harvest_lock. Split out only so the lock/event-recording
    wrapper above has a single, simple call site."""
    report = evaluate_branch(config, project, task_id, on_gate=_set_progress_stage)
    if not report["harvestable"]:
        report["merged"] = False
        return report

    repo_path = project["path"]
    branch = report["branch"]
    base_branch = spawn.current_branch(repo_path)

    # Final immediate pre-merge safety check. Gates 4/5 evaluate in an
    # isolated scratch worktree and never look at the project's own
    # checkout, so its cleanliness could have changed since. Unlike the
    # old "refuse on ANY uncommitted change" rule, this matches git's
    # own real tolerance (see _parse_dirty_paths/_merge_touched_paths):
    # only a staged change anywhere, or an unstaged/untracked change
    # that overlaps with what this merge would actually touch, blocks
    # it -- unrelated dirt merges straight past, same as git itself
    # would allow.
    _set_progress_stage(STAGE_MAIN_CHECKOUT_CLEAN)
    status_proc = server.run_git(["status", "--porcelain"], cwd=repo_path)
    if status_proc.returncode != 0:
        stderr = (status_proc.stderr or status_proc.stdout or "unknown error").strip()
        report["gates"].append(_gate("mainCheckoutClean", False, f"failed to check {project_name}'s checkout: {stderr}"))
        report["harvestable"] = False
        report["merged"] = False
        return report

    staged_paths, other_paths = _parse_dirty_paths(status_proc.stdout or "")
    touched_paths = _merge_touched_paths(repo_path, base_branch, branch)
    if touched_paths is None:
        report["gates"].append(_gate(
            "mainCheckoutClean", False,
            f"failed to determine which files merging {branch} into {base_branch} would touch",
        ))
        report["harvestable"] = False
        report["merged"] = False
        return report

    # Staged changes block unconditionally (even to a path the merge
    # never touches); an unstaged/untracked path only blocks when it
    # overlaps with the merge's own diff -- see _parse_dirty_paths.
    blocking_paths = staged_paths | (other_paths & touched_paths)
    if blocking_paths:
        # The only automatic cleanup offer is the exact, unstaged tracked
        # task file whose branch version will supersede the board edit.
        # Any staged path or any additional blocking path keeps the ordinary
        # refusal and is never touched by Centrale.
        task_path = _safe_task_path(report.get("taskPath"), task_id)
        unstaged_tracked = _unstaged_tracked_paths(status_proc.stdout or "")
        if task_path and blocking_paths == {task_path} and task_path in unstaged_tracked:
            report["discardableMainTaskEdit"] = {"path": task_path}
        file_list = ", ".join(sorted(blocking_paths))
        n = len(blocking_paths)
        report["gates"].append(_gate(
            "mainCheckoutClean", False,
            f"{project_name}'s checkout has uncommitted changes that would conflict with this merge -- "
            f"commit or stash these {n} file{'s' if n != 1 else ''}: {file_list}",
        ))
        report["harvestable"] = False
        report["merged"] = False
        return report
    report["gates"].append(_gate("mainCheckoutClean", True, None))

    # Everything left in other_paths at this point is unrelated dirt
    # that's safe to merge straight past -- noted on the success report
    # below (see harvest_branch's docstring) once the merge actually
    # happens, not claimed here before it does.
    unrelated_dirty_count = len(other_paths)

    _set_progress_stage(STAGE_MERGE)
    title = report.get("taskTitle") or task_id
    merge_proc = server.run_git(
        ["merge", "--no-ff", "--no-edit", "-m", f"Merge {branch}: {title}", branch], cwd=repo_path
    )
    if merge_proc.returncode != 0:
        stderr = (merge_proc.stderr or merge_proc.stdout or "merge failed").strip()
        server.run_git(["merge", "--abort"], cwd=repo_path)
        raise HarvestError(f"merge of {branch} into {base_branch} failed unexpectedly: {stderr}", status=500)

    warnings = []
    wt_dir = spawn.worktree_dir(config, project_name, task_id)
    checkout = report.get("branchCheckout") or {}
    if checkout.get("kind") != "none":
        remove_proc = server.run_git(
            ["worktree", "remove", "--force", wt_dir], cwd=repo_path
        )
        if remove_proc.returncode != 0:
            stderr = (remove_proc.stderr or remove_proc.stdout or "unknown error").strip()
            warnings.append(
                f"merge succeeded but failed to remove worktree {wt_dir}: {stderr}"
            )

    branch_del_proc = server.run_git(["branch", "-d", branch], cwd=repo_path)
    if branch_del_proc.returncode != 0:
        stderr = (branch_del_proc.stderr or branch_del_proc.stdout or "unknown error").strip()
        warnings.append(f"merge succeeded but failed to delete branch {branch}: {stderr}")

    report["merged"] = True
    report["baseBranch"] = base_branch
    if unrelated_dirty_count:
        report["unrelatedDirtyCount"] = unrelated_dirty_count
    if warnings:
        report["warnings"] = warnings
    return report


def harvest_all_ready(config, project_name, trigger="click"):
    """Harvests every currently-harvestable task/<id> branch in a
    project, one at a time -- re-evaluating each branch's gates fresh
    immediately before merging it (via harvest_branch, which always
    re-evaluates), since merging one branch moves the project's base
    branch forward and can change whether a later branch's merge is
    still clean. Raises HarvestError only for an unknown project.

    This is the exact function the background auto-harvest thread calls
    (with trigger="auto") for every configured project each cycle it's
    live -- the "one click can harvest all green branches" UI action and
    auto-harvest share this one code path, never separate merge logic.

    Returns {"merged": [...], "notReady": [...]} -- each a list of
    per-branch reports in the same shape harvest_branch() returns.
    """
    project = _find_project(config, project_name)
    if project is None:
        raise HarvestError(f"unknown project: {project_name}", status=404)
    repo_path = project.get("path") or ""
    if not os.path.isdir(repo_path):
        raise HarvestError(f"project path not found: {repo_path or '(empty)'}", status=502)

    merged = []
    not_ready = []
    for branch in list_task_branches(repo_path):
        task_id = task_id_from_branch(branch)
        if task_id is None:
            continue
        report = harvest_branch(config, project_name, task_id, trigger=trigger)
        if report.get("merged"):
            merged.append(report)
        else:
            not_ready.append(report)
    return {"merged": merged, "notReady": not_ready}


class AutoHarvestThread(threading.Thread):
    """Background evaluator for harvest.mode == "auto": every `interval`
    seconds, re-reads the *live* mode from `config` and, only when it's
    currently "auto", runs harvest_all_ready() -- the exact same
    evaluate-then-merge path a click uses -- for every configured
    project. On any other mode this cycle does nothing at all (no gate
    evaluation, no merging), but the thread itself keeps looping and
    re-checking regardless of mode, so a live settings change (see
    task-20) takes effect on the very next cycle without a server
    restart. Always a daemon thread; one bad cycle, or one project's
    HarvestError, never kills the loop.

    `sleep_fn` is injectable so tests can drive cycles without a real
    wait. `run_cycle()` is also directly callable on its own for fully
    synchronous, hermetic tests that never touch real threading at all.
    """

    def __init__(self, config, interval=DEFAULT_AUTO_HARVEST_INTERVAL, sleep_fn=None):
        super().__init__(name="centrale-auto-harvest", daemon=True)
        self.config = config
        self.interval = interval
        self._sleep_fn = sleep_fn or time.sleep
        self._stop_event = threading.Event()

    def stop(self):
        self._stop_event.set()

    def run(self):
        while not self._stop_event.is_set():
            self._sleep_fn(self.interval)
            if self._stop_event.is_set():
                return
            try:
                self.run_cycle()
            except Exception:
                pass  # never let one bad cycle end the background thread

    def run_cycle(self):
        """One evaluation pass over every configured project, or a no-op
        if the live mode isn't "auto" right now. Public and synchronous
        so tests can call it directly."""
        mode = (self.config.get("harvest") or {}).get("mode", "click")
        if mode != "auto":
            return
        for project in self.config.get("projects", []):
            name = project.get("name")
            if not name:
                continue
            try:
                harvest_all_ready(self.config, name, trigger="auto")
            except HarvestError:
                continue  # one project's problem shouldn't block the others


def start_auto_harvest_thread(config, interval=DEFAULT_AUTO_HARVEST_INTERVAL, sleep_fn=None):
    """Starts (and returns) the background auto-harvest thread. Always
    started regardless of the current harvest.mode -- see
    AutoHarvestThread's docstring for why."""
    thread = AutoHarvestThread(config, interval=interval, sleep_fn=sleep_fn)
    thread.start()
    return thread
