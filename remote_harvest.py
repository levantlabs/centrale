"""Merge gates, Discard and Abandon for a task on a machine (doc-3,
"Merge" and "Discard and Abandon"; task-208).

A remote task's session, worktree and branch live on the worker, so
every gate and every survey is read THERE, never inferred from the hub
having no local worktree (which would make "worktree clean" pass as "no
worktree", and "no live session" pass because the hub's tmux has none):

- one round trip surveys the worker: its centrale-* sessions, $HOME, the
  project repository's checkouts, the branch tip and the task worktree's
  `git status --porcelain`;
- the hub then fetches the branch into refs/remotes/<machine>/task/<id>
  and pins the surveyed tip -- a fetch that brings anything else back is
  refused, since that is not the commit the survey described;
- "task done" reads the pinned commit through a detached snapshot on the
  hub (the fetched branch, as doc-3 says);
- "merges cleanly" and checkCommand run on the worker in a throwaway
  detached scratch worktree cut at the hub's base commit, pushed there
  and pinned first; the worker refuses when its base or the branch is not
  at the pinned commits, and checkCommand runs under `timeout -k` there
  (a hub-side timeout kills only the ssh client, task-205) with a sweep
  of the scratch directory afterwards for anything that escaped its
  process group;
- the merge itself happens on the hub, of the pinned commit, only after
  the base and the worker's tip are re-read and found unmoved.

Discard and Abandon fetch first and survey the worker under the task's
lifecycle lock, refuse when the confirm no longer describes what is
there (as on the hub, task-121), cut the recovery tag on the HUB from the
fetched tip, and only then remove anything on the worker -- with a
compare-and-delete of the branch there.

A worker that does not answer refuses every one of these with a stated
reason; nothing is merged, tagged or destroyed on a guess.

Only ever imported for a project with `machines` (the single-machine
guarantee); every function here takes the project placed at the task's
derived location (machines.placed, task-224). Python 3.12 stdlib only.
"""

from __future__ import annotations

import time

import harvest
import machines
import remote
import server
import spawn
from machines import ANSWERED, UNREACHED

SpawnError = spawn.SpawnError

# The grace `timeout -k` gives a timed-out checkCommand between TERM and
# KILL, and the slack the round trip carrying it gets on top of both.
CHECK_KILL_GRACE_SECONDS = 10
SCRATCH_SLACK_SECONDS = 120

# The pseudo-gate a remote merge appends after the five gates: the base
# and the branch tip are still the commits the gates evaluated.
STAGE_PINS_UNCHANGED = "pinsUnchanged"

# Empty output (exit 0) when the project repository or the branch is absent.
_TIP_SCRIPT = ('test -d "$1" || exit 0; '
               'exec git --git-dir="$1" for-each-ref --format="%(objectname)" "refs/heads/$2"')

# The task worktree's uncommitted paths, or a marker when there is none.
_STATUS_SCRIPT = '[ -d "$1" ] || { echo centrale-no-worktree; exit 0; }; exec git -C "$1" status --porcelain'
_NO_WORKTREE = "centrale-no-worktree"

# Gates 4 and 5 on the worker. $1 bare, $2 base branch, $3 pinned base
# sha, $4 task branch, $5 pinned tip, $6 check timeout, $7 kill grace, $8
# the sweep program, $9 the checkCommand ("" for none). The scratch
# worktree is detached at the pinned base, so neither the agent's branch
# nor the project repository's base branch is ever touched, and it is
# removed -- after a sweep of anything still running in it -- on every
# exit. The check's output goes to a file, not a pipe: a child that
# escaped the process group would otherwise hold the pipe open.
_SCRATCH_SCRIPT = r'''
bare=$1 base=$2 basesha=$3 branch=$4 tip=$5 limit=$6 grace=$7 sweep=$8 check=$9
now=$(git --git-dir="$bare" for-each-ref --format='%(objectname)' "refs/heads/$base")
[ "$now" = "$basesha" ] || { echo "centrale-base-moved $now"; exit 20; }
now=$(git --git-dir="$bare" for-each-ref --format='%(objectname)' "refs/heads/$branch")
[ "$now" = "$tip" ] || { echo "centrale-tip-moved $now"; exit 21; }
scratch=$(mktemp -d "${TMPDIR:-/tmp}/centrale-harvest-XXXXXX") || exit 22
log=$(mktemp "${TMPDIR:-/tmp}/centrale-check-XXXXXX") || { rmdir "$scratch"; exit 22; }
cleanup() {
  cd /
  python3 -c "$sweep" "$scratch" kill >/dev/null 2>&1
  git --git-dir="$bare" worktree remove --force "$scratch" >/dev/null 2>&1
  rm -rf "$scratch" "$log"
  git --git-dir="$bare" worktree prune >/dev/null 2>&1
}
trap cleanup EXIT
rmdir "$scratch"
git --git-dir="$bare" worktree add --detach "$scratch" "$basesha" >"$log" 2>&1 || { cat "$log" >&2; exit 22; }
cd "$scratch" || exit 22
if ! git -c user.name=Centrale -c user.email=centrale@localhost merge --no-ff --no-edit "$tip" >"$log" 2>&1; then
  cat "$log" >&2
  git merge --abort >/dev/null 2>&1
  exit 23
fi
echo "centrale-merged"
[ -n "$check" ] || exit 0
timeout -k "$grace" "$limit" sh -c "$check" </dev/null >"$log" 2>&1
rc=$?
echo "centrale-check-begin"
tail -c 2000 "$log"
echo
echo "centrale-check-rc $rc"
exit 0
'''

# Discard (or Abandon) on the worker, after the hub has tagged the tip.
# $1 bare, $2 worktree, $3 branch, $4 the surveyed tip ("" for none), $5
# discard|abandon, $6 the task session, then the tmux argv. Re-checks the
# session and the tip immediately before the first removal; the branch
# goes by compare-and-delete, so it is deleted only at the tagged tip.
_THROWAWAY_SCRIPT = r'''
bare=$1 wt=$2 branch=$3 tip=$4 mode=$5 name=$6; shift 6
if "$@" has-session -t "=$name" 2>/dev/null; then echo "centrale-live"; exit 30; fi
now=$(git --git-dir="$bare" for-each-ref --format='%(objectname)' "refs/heads/$branch")
[ "$now" = "$tip" ] || { echo "centrale-moved $now"; exit 31; }
if [ -d "$wt" ]; then
  git --git-dir="$bare" worktree remove --force "$wt" >&2 || exit 32
  echo "centrale-removed"
fi
git --git-dir="$bare" worktree prune
if [ "$mode" = discard ] && [ -n "$tip" ]; then
  git --git-dir="$bare" update-ref -d "refs/heads/$branch" "$tip" || exit 33
  echo "centrale-branch-deleted"
fi
exit 0
'''


def _machine(config, project):
    return remote.machine_for(config, project)


def _bare(machine, project):
    return machines.repo_git_dir_path(machine, project)


def _error(result):
    return (result.stderr or result.stdout or "").strip() or result.status


# ---------------------------------------------------------------------------
# Reading the worker
# ---------------------------------------------------------------------------

def survey(config, machine, project, task_id):
    """One round trip: what the gates and the throwaway actions need from
    the worker. Raises SpawnError (503 unreached, 504 unknown, 502 a
    failed read) when it did not answer cleanly.

    Returns {"machine", "live" (session name or None), "home", "checkout"
    ({"kind": "centrale"|"external"|"none", "path"} -- external paths
    as "<machine>:<path>"), "tip" (sha or None), "worktreePath",
    "worktreeExists", "dirtyPaths" (None when unreadable)}."""
    name = machine["name"]
    branch = spawn.branch_name(task_id)
    bare = _bare(machine, project)
    wt = remote.worktree_path(machine, project["name"], task_id)
    steps = ("list sessions", "read home", "list worktrees", "read the task branch",
             "read the worktree status")
    results = machines.run_batch(config, name, [
        (remote._sh(remote._SESSIONS_SCRIPT, *remote.tmux_argv(machine)), None),
        (remote._sh(remote._HOME_SCRIPT), None),
        (remote._sh(machines._WORKTREE_LIST, bare), None),
        (remote._sh(_TIP_SCRIPT, bare, branch), None),
        (remote._sh(_STATUS_SCRIPT, wt), None),
    ])
    for step, result in zip(steps[:4], results[:4]):
        if not result.ok:
            raise remote._step_error(name, step, result)
    status = results[4]
    if status.status != ANSWERED:
        raise remote._step_error(name, steps[4], status)

    home = results[1].stdout.strip()
    expected = remote.expand(wt, home)
    path = spawn.parse_worktree_checkouts(results[2].stdout).get(branch)
    if path is None:
        checkout = {"kind": "none", "path": None}
    elif path.rstrip("/") == expected.rstrip("/"):
        checkout = {"kind": "centrale", "path": expected}
    else:
        checkout = {"kind": "external", "path": f"{name}:{path}"}

    exists = status.stdout.strip() != _NO_WORKTREE
    if not exists:
        dirty = []
    elif status.returncode == 0:
        dirty = server._dirty_paths_from_status(status.stdout)
    else:
        dirty = None
    session = spawn.session_name(project["name"], task_id)
    sessions = {s["name"] for s in remote._parse_sessions(results[0].stdout)}
    return {
        "machine": name,
        "live": session if session in sessions else None,
        "home": home,
        "checkout": checkout,
        "tip": results[3].stdout.strip() or None,
        "worktreePath": expected,
        "worktreeExists": exists,
        "dirtyPaths": dirty,
    }


def fetch_pinned(config, machine, project, task_id, tip):
    """Fetch the branch into the hub's tracking ref and confirm it is the
    surveyed tip. Returns an error sentence, or None once the hub holds
    exactly `tip` there."""
    branch = spawn.branch_name(task_id)
    ref = remote.tracking_ref(machine["name"], task_id)
    url = remote.git_url(machine, _bare(machine, project))
    proc = remote.run_hub_git(
        ["fetch", "--quiet", "--no-write-fetch-head", url, f"+refs/heads/{branch}:{ref}"],
        project["path"])
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or f"git fetch exited {proc.returncode}").strip()
        return f"machine {machine['name']}: fetching {branch} failed: {detail}"
    fetched = server.run_git(["rev-parse", "--verify", "--quiet", ref], cwd=project["path"])
    sha = (fetched.stdout or "").strip() if fetched.returncode == 0 else ""
    if sha != tip:
        return (f"{branch} moved on machine {machine['name']} while it was being fetched "
                f"(fetched {sha[:10] or 'nothing'}, surveyed {tip[:10]})")
    return None


def _worker_tip(config, machine, project, task_id):
    """(sha or None, error or None): the branch tip in the project repository."""
    result = machines.run_on_machine(config, machine["name"], remote._sh(
        _TIP_SCRIPT, _bare(machine, project), spawn.branch_name(task_id)))
    if not result.ok:
        return None, remote._step_error(machine["name"], "read the task branch", result).message
    return result.stdout.strip() or None, None


def already_merged(project, task_id, task_status):
    """The board's alreadyMerged for a task on a machine (task-45's rule):
    Done on main and the fetched copy an ancestor of the base branch."""
    return server._branch_already_merged(
        project["path"], task_id, task_status,
        ref=remote.tracking_ref(project["machine"], task_id))


# ---------------------------------------------------------------------------
# The gates
# ---------------------------------------------------------------------------

def _push_pinned_base(config, machine, project):
    """Pin the hub's base branch and push exactly that commit to the
    worker. Returns (base_branch, sha, error)."""
    repo = project["path"]
    base = spawn.current_branch(repo)
    if base == "HEAD":
        return base, None, f"{repo} has a detached HEAD: no base branch to merge into"
    proc = server.run_git(["rev-parse", "--verify", "--quiet", f"refs/heads/{base}"], cwd=repo)
    sha = (proc.stdout or "").strip()
    if proc.returncode != 0 or not sha:
        return base, None, f"failed to read {base} on the hub"
    url = remote.git_url(machine, _bare(machine, project))
    # Not forced: the worker's copy only ever moves forward from the hub's.
    pushed = remote.run_hub_git(["push", "--quiet", url, f"{sha}:refs/heads/{base}"], repo)
    if pushed.returncode != 0:
        detail = (pushed.stderr or pushed.stdout or "git push failed").strip()
        return base, sha, (f"machine {machine['name']}: pushing {base} at {sha[:10]} to its bare "
                           f"repository failed: {detail}")
    return base, sha, None


def _check_timeout(project):
    return project.get("checkTimeoutSeconds") or server.DEFAULT_CHECK_TIMEOUT_SECONDS


def scratch_merge_and_check(config, machine, project, task_id, base, base_sha, tip, on_check=None):
    """Gates 4 and 5 on the worker. Returns (merge_ok, merge_reason,
    check_ok_or_None, check_reason), as harvest._scratch_merge_and_check.
    `on_check` is announced once the worker reports the trial merge clean
    and there is a command to run -- after the fact, since both happen in
    one round trip, and never for a project without a checkCommand."""
    name = machine["name"]
    check = project.get("checkCommand") or ""
    limit = int(_check_timeout(project))
    result = machines.run_on_machine(
        config, name,
        remote._sh(_SCRATCH_SCRIPT, _bare(machine, project), base, base_sha,
                   spawn.branch_name(task_id), tip, str(limit), str(CHECK_KILL_GRACE_SECONDS),
                   remote.SWEEP_PROGRAM, check),
        timeout=limit + CHECK_KILL_GRACE_SECONDS + SCRATCH_SLACK_SECONDS)
    if result.status != ANSWERED:
        return False, remote._step_error(name, "run the trial merge", result).message, None, None
    rc = result.returncode
    if rc == 20:
        now = remote._marker(result.stdout, "centrale-base-moved") or "nothing"
        return False, (f"the base moved: {base} on machine {name} is at {now[:10]}, not the pinned "
                       f"{base_sha[:10]} -- refusing to test against a different base"), None, None
    if rc == 21:
        now = remote._marker(result.stdout, "centrale-tip-moved") or "nothing"
        return False, (f"{spawn.branch_name(task_id)} moved on machine {name} since it was pinned "
                       f"(now {now[:10]}, pinned {tip[:10]})"), None, None
    if rc == 22:
        return False, f"failed to create a scratch worktree on machine {name}: {_error(result)}", None, None
    if rc == 23:
        return False, f"merge conflict: {(result.stderr or 'merge failed').strip()}", None, None
    if rc != 0 or remote._marker(result.stdout, "centrale-merged") is None:
        return False, f"machine {name}: the trial merge failed (exit {rc}): {_error(result)}", None, None
    if not check:
        return True, None, True, None
    if on_check is not None:
        on_check()
    check_rc = remote._marker(result.stdout, "centrale-check-rc")
    if check_rc is None or not check_rc.lstrip("-").isdigit():
        return True, None, False, f"checkCommand on machine {name}: no exit status came back"
    check_rc = int(check_rc)
    if check_rc == 0:
        return True, None, True, None
    if check_rc in (124, 137):
        return True, None, False, f"checkCommand timed out after {limit}s on machine {name} (killed there)"
    output = result.stdout.split("centrale-check-begin\n", 1)[-1].rsplit("centrale-check-rc", 1)[0]
    return True, None, False, f"checkCommand failed on machine {name}: {output.strip()[-500:] or 'no output'}"


def evaluate(config, project, task_id, on_gate=None, pins=None):
    """harvest.evaluate_branch for a task on a machine: the same five
    gates and report shape, plus "machine". `pins`, when given, receives
    {"tip", "base", "baseBranch"} -- the commits the gates evaluated --
    for harvest_locked to hold the merge to."""
    machine = _machine(config, project)
    name = machine["name"]
    branch = spawn.branch_name(task_id)
    names = harvest.GATE_NAMES

    def began(gate):
        if on_gate is not None:
            on_gate(gate)

    def report(gates, title=None, path=None, checkout=None):
        out = harvest._report(task_id, branch, gates, title, path, checkout)
        out["machine"] = name
        return out

    def stop(gates, index, reason, title=None, path=None, checkout=None):
        gates.append(harvest._gate(names[index], False, reason))
        gates += [harvest._skipped(n) for n in names[index + 1:]]
        return report(gates, title, path, checkout)

    began(names[0])
    try:
        found = survey(config, machine, project, task_id)
    except SpawnError as exc:
        return stop([], 0, f"{exc.message} -- its sessions and worktree cannot be checked, so "
                           "nothing is merged")
    if found["tip"] is None:
        out = report([])
        out["alreadyMerged"] = True
        return out
    gates = []
    if found["live"]:
        return stop(gates, 0, f"a live tmux session is still running on machine {name}: {found['live']}")
    gates.append(harvest._gate(names[0], True, None))

    began(names[1])
    checkout = found["checkout"]
    tip = found["tip"]
    task = None
    if checkout["kind"] == "external":
        return stop(gates, 1, harvest._missing_worktree_reason(branch, checkout), checkout=checkout)
    if checkout["kind"] == "centrale" and not found["worktreeExists"]:
        return stop(gates, 1, harvest._missing_worktree_reason(branch, checkout), checkout=checkout)
    error = fetch_pinned(config, machine, project, task_id, tip)
    if error:
        return stop(gates, 1, error, checkout=checkout)
    with spawn.detached_snapshot(project["path"], tip) as (tmp_dir, snap_error):
        if snap_error is not None:
            ok, reason = False, (f"failed to read task {task_id} from the fetched {branch}: could not "
                                 f"create a detached snapshot: {snap_error}")
        else:
            ok, reason, task = harvest._gate_task_done(tmp_dir, task_id)
    title = task.get("title") if task else None
    path = harvest._safe_task_path(task.get("path"), task_id) if task else None
    if not ok:
        return stop(gates, 1, reason, title, path, checkout)
    gates.append(harvest._gate(names[1], True, None))

    began(names[2])
    clean_reason = None
    if found["worktreeExists"]:
        dirty = found["dirtyPaths"]
        if dirty is None:
            return stop(gates, 2, f"failed to check the worktree status on machine {name}",
                        title, path, checkout)
        if dirty:
            n = len(dirty)
            listed = ", ".join(dirty[:5]) + (f" (+{n - 5} more)" if n > 5 else "")
            return stop(gates, 2, (f"worktree on machine {name} has uncommitted changes "
                                   f"({n} file{'s' if n != 1 else ''}: {listed})"),
                        title, path, checkout)
    else:
        clean_reason = f"no worktree on machine {name} -- nothing uncommitted to protect"
    base_branch = spawn.current_branch(project["path"])
    count = server._branch_commit_count(project["path"], tip, base_branch)
    if count is None:
        return stop(gates, 2, f"failed to compare {branch} to {base_branch}", title, path, checkout)
    if count == 0:
        return stop(gates, 2, f"nothing to merge — no commits beyond {base_branch}", title, path, checkout)
    gates.append(harvest._gate(names[2], True, clean_reason))

    began(names[3])
    base_branch, base_sha, error = _push_pinned_base(config, machine, project)
    if error:
        gates.append(harvest._gate(names[3], False, error))
        gates.append(harvest._skipped(names[4]))
        return report(gates, title, path, checkout)
    merge_ok, merge_reason, check_ok, check_reason = scratch_merge_and_check(
        config, machine, project, task_id, base_branch, base_sha, tip,
        on_check=lambda: began(names[4]))
    gates.append(harvest._gate(names[3], merge_ok, merge_reason))
    if merge_ok:
        gates.append(harvest._gate(names[4], check_ok, check_reason))
    else:
        gates.append(harvest._skipped(names[4]))
    out = report(gates, title, path, checkout)
    if pins is not None:
        pins.update(tip=tip, base=base_sha, baseBranch=base_branch)
    if not (merge_ok and check_ok):
        return harvest._annotate_behind_base(out, project["path"], base_branch, tip)
    return out


def _refuse(report, reason):
    report["gates"].append(harvest._gate(STAGE_PINS_UNCHANGED, False, reason))
    report["harvestable"] = False
    report["merged"] = False
    return report


def harvest_locked(config, project, project_name, task_id):
    """harvest._harvest_branch_locked for a task on a machine, under the
    harvest lock and the task's lifecycle lock: the gates, then the base
    and the worker's tip re-read and held to the pinned commits, the
    hub's checkout check, the merge of the pinned commit on the hub,
    `main` pushed back to the worker, and the worker's worktree and
    branch removed there (recorded as owed when it does not answer)."""
    pins = {}
    report = evaluate(config, project, task_id, on_gate=harvest._set_progress_stage, pins=pins)
    if not report["harvestable"]:
        report["merged"] = False
        return report

    machine = _machine(config, project)
    name = machine["name"]
    repo = project["path"]
    branch = report["branch"]
    base, base_sha, tip = pins["baseBranch"], pins["base"], pins["tip"]

    harvest._set_progress_stage(STAGE_PINS_UNCHANGED)
    now = server.run_git(["rev-parse", "--verify", "--quiet", f"refs/heads/{base}"], cwd=repo)
    if spawn.current_branch(repo) != base or (now.stdout or "").strip() != base_sha:
        return _refuse(report, (f"{base} moved on the hub since the gates ran (they tested "
                                f"{base_sha[:10]}); merge again to test the new base"))
    worker_tip, error = _worker_tip(config, machine, project, task_id)
    if error:
        return _refuse(report, f"{error} -- cannot confirm {branch} is still at the tested commit")
    if worker_tip != tip:
        return _refuse(report, (f"{branch} moved on machine {name} since the gates ran (now "
                                f"{(worker_tip or 'gone')[:10]}, tested {tip[:10]})"))
    report["gates"].append(harvest._gate(STAGE_PINS_UNCHANGED, True, None))

    unrelated = harvest.main_checkout_gate(report, project, task_id, base, tip)
    if unrelated is None:
        return report

    harvest._set_progress_stage(harvest.STAGE_MERGE)
    title = report.get("taskTitle") or task_id
    merge_proc = server.run_git(
        ["merge", "--no-ff", "--no-edit", "-m", f"Merge {branch}: {title}", tip], cwd=repo)
    if merge_proc.returncode != 0:
        stderr = (merge_proc.stderr or merge_proc.stdout or "merge failed").strip()
        server.run_git(["merge", "--abort"], cwd=repo)
        raise harvest.HarvestError(
            f"merge of {branch} ({tip[:10]}) into {base} failed unexpectedly: {stderr}", status=500)
    spawn.unlock_task_file(repo, task_id)

    warnings = []
    url = remote.git_url(machine, _bare(machine, project))
    pushed = remote.run_hub_git(["push", "--quiet", url, f"refs/heads/{base}:refs/heads/{base}"], repo)
    if pushed.returncode != 0:
        warnings.append(f"merged, but pushing {base} to machine {name} failed: "
                        f"{(pushed.stderr or pushed.stdout).strip()}")
    result = machines.run_on_machine(config, name, remote._cleanup_args(machine, project, task_id, tip, True))
    if result.status != ANSWERED:
        entry = remote.record_owed({"kind": "cleanup", "project": project_name, "taskId": task_id,
                                    "machine": name, "requestedAt": time.time(),
                                    "expectedTip": tip, "force": True, "reason": _error(result)})
        warnings.append(f"merged, but machine {name} did not confirm removing the worktree and branch "
                        f"({_error(result)}); that is recorded as owed (id {entry['id']}) and retried")
    elif result.returncode == 0:
        remote._forget_on_hub(project, machine, task_id)
    else:
        warnings.append(f"merged, but removing the worktree and branch on machine {name} failed "
                        f"(exit {result.returncode}): {_error(result)}")

    report["merged"] = True
    report["baseBranch"] = base
    if unrelated:
        report["unrelatedDirtyCount"] = unrelated
    if warnings:
        report["warnings"] = warnings
    return report


# ---------------------------------------------------------------------------
# Discard and Abandon (task-119's routes, for a task on a machine)
# ---------------------------------------------------------------------------

def attempt_survey(config, project, task_id):
    """server._survey_attempt's shape for a task on a machine: the worker
    surveyed, then the branch fetched and pinned, the commit count taken
    on the hub against the fetched copy. Returns (attempt, survey);
    raises SpawnError when the worker did not answer or the fetch failed."""
    machine = _machine(config, project)
    found = survey(config, machine, project, task_id)
    tip = found["tip"]
    repo = project["path"]
    base = spawn.current_branch(repo)
    count = None
    if tip:
        error = fetch_pinned(config, machine, project, task_id, tip)
        if error:
            raise SpawnError(error, status=502)
        count = server._branch_commit_count(repo, tip, base)
    dirty = found["dirtyPaths"]
    attempt = {
        "branch": spawn.branch_name(task_id),
        "branchExists": tip is not None,
        "branchTip": tip,
        "baseBranch": base,
        "commitCount": count,
        "worktreePath": found["worktreePath"],
        "worktreeExists": found["worktreeExists"],
        "dirtyPaths": dirty,
        "dirtyFileCount": None if dirty is None else len(dirty),
        "machine": machine["name"],
    }
    return attempt, found


def _unreached(exc, what):
    return exc.status, {"error": f"{exc.message} -- {what}"}


def discard_preview(config, project, task_id):
    """GET /api/discard-preview for a task on a machine. Returns
    (status, body)."""
    try:
        attempt, found = attempt_survey(config, project, task_id)
    except SpawnError as exc:
        return _unreached(exc, "what Discard or Abandon would destroy cannot be measured there")
    if not attempt["branchExists"] and not attempt["worktreeExists"]:
        return 404, {"error": f"no worktree or branch found for {project['name']}/{task_id} on machine "
                              f"{attempt['machine']}"}
    body = dict(attempt)
    body["liveSession"] = found["live"]
    body["externalCheckout"] = None
    if found["checkout"]["kind"] == "external":
        body["externalCheckout"] = {
            "path": found["checkout"]["path"],
            "reason": spawn.external_checkout_reason(attempt["branch"], found["checkout"]["path"]),
        }
    body["recoveryCommand"] = (server.recovery_command(attempt["branch"], attempt["branchTip"])
                               if attempt["branchTip"] else None)
    return 200, body


def throw_away(config, project, task_id, expected_tip, expected_paths, mode):
    """POST /api/discard-attempt (mode "discard") or /api/abandon-worktree
    ("abandon") for a task on a machine, under the task's lifecycle lock
    (the caller holds it). Returns (status, body)."""
    machine = _machine(config, project)
    name = machine["name"]
    repo = project["path"]
    try:
        attempt, found = attempt_survey(config, project, task_id)
    except SpawnError as exc:
        return _unreached(exc, "nothing was discarded")
    branch = attempt["branch"]
    if mode == "abandon" and not attempt["worktreeExists"]:
        return 404, {"error": f"no worktree found for {project['name']}/{task_id} on machine {name}"}
    if not attempt["branchExists"] and not attempt["worktreeExists"]:
        return 404, {"error": f"no worktree or branch found for {project['name']}/{task_id} on machine {name}"}
    if attempt["worktreeExists"] and attempt["dirtyPaths"] is None:
        return 500, {"error": f"failed to check worktree status in {attempt['worktreePath']} on machine {name}"}
    if found["live"]:
        return 409, {"error": f"a live tmux session is still running on machine {name}: {found['live']}"}
    if found["checkout"]["kind"] == "external":
        return 409, {"error": spawn.external_checkout_reason(branch, found["checkout"]["path"])}
    stale = server.stale_state_reason(attempt, expected_tip, expected_paths)
    if stale:
        return 409, {"error": stale}

    tip = attempt["branchTip"]
    tag = None
    if mode == "discard" and tip:
        # On the hub, from the fetched tip, before anything is removed:
        # the worker's reflogs go with its worktree and branch.
        tag = server.recovery_tag_name(task_id)
        tagged = server.run_git(["tag", tag, tip], cwd=repo)
        if tagged.returncode != 0:
            return 500, {"error": f"failed to tag {tip} as {tag}: {_error(tagged)}"}

    result = machines.run_on_machine(config, name, remote._sh(
        _THROWAWAY_SCRIPT, _bare(machine, project),
        remote.worktree_path(machine, project["name"], task_id), branch, tip or "", mode,
        spawn.session_name(project["name"], task_id), *remote.tmux_argv(machine)))
    kept = f"; the recovery tag {tag} is on the hub" if tag else ""
    if result.status != ANSWERED:
        status = 503 if result.status == UNREACHED else 504
        what = "nothing was removed there" if result.status == UNREACHED else "the outcome there is unknown"
        return status, {"error": f"machine {name} did not answer ({_error(result)}): {what}{kept}"}
    if result.returncode == 30:
        return 409, {"error": f"a live tmux session started on machine {name}; nothing was removed{kept}"}
    if result.returncode == 31:
        now = remote._marker(result.stdout, "centrale-moved") or "gone"
        return 409, {"error": (f"the repository changed after that preview -- {branch} on machine {name} is "
                               f"at {now[:10] or 'nothing'} now. Nothing was discarded{kept}.")}
    if result.returncode != 0:
        return 500, {"error": f"machine {name}: removing the attempt failed (exit {result.returncode}): "
                              f"{_error(result)}{kept}"}

    if mode == "discard":
        server.run_git(["update-ref", "-d", remote.tracking_ref(name, task_id)], cwd=repo)
    unlocked = spawn.unlock_task_file(repo, task_id)
    body = {
        "ok": True,
        "branch": branch,
        "branchTip": tip,
        "baseBranch": attempt["baseBranch"],
        "commitCount": attempt["commitCount"],
        "discardedPaths": attempt["dirtyPaths"] or [],
        "taskFileUnlocked": unlocked,
        "machine": name,
    }
    if mode == "discard":
        body.update(
            branchDeleted=remote._marker(result.stdout, "centrale-branch-deleted") is not None,
            worktreeRemoved=attempt["worktreeExists"],
            recoveryTag=tag,
            recoveryCommand=server.recovery_command(branch, tip) if tip else None,
        )
    else:
        body.update(branchKept=attempt["branchExists"], worktreeRemoved=True)
    return 200, body


# ---------------------------------------------------------------------------
# The board: where each task is, and where its branch is checked out
# ---------------------------------------------------------------------------

def refresh_reads(config):
    """The reads a board refresh adds to each machine's one round trip
    (machines.refresh): the worker's $HOME, then the worktree list in the
    project repository of every project that lists the machine, in
    projects_on order."""
    reads = {}
    for machine in config.get("machines") or []:
        projects = machines.projects_on(config, machine["name"])
        if projects:
            reads[machine["name"]] = [(remote._sh(remote._HOME_SCRIPT), None)] + [
                machines.checkouts_command(config, p, machine["name"]) for p in projects]
    return reads


class BoardPlacement:
    """One board refresh's evidence of where a project's tasks are
    (task-224), read once per project: the hub's local task branches and
    worktrees, the hub's fetched refs from each of the project's
    machines, and each machine's project-repository worktree list from the
    refresh's one round trip. locate() is then machines.decide per task,
    with no further command."""

    def __init__(self, config, project, report):
        self.config = config
        self.project = project
        repo = project["path"]
        self.hub = set(harvest.list_task_branches(repo)) | {
            b for b in spawn.branch_checkouts(repo) if b.startswith("task/")}
        rows = {r["name"]: r for r in report}
        self.machines = {}
        for name in project.get("machines") or []:
            fetched = machines.fetched_task_branches(project, name)
            row = rows.get(name) or {}
            results = row.get("results") or []
            listed = [p["name"] for p in machines.projects_on(config, name)]
            index = listed.index(project["name"]) + 1 if project["name"] in listed else None
            answered = (index is not None and len(results) > index
                        and results[0].ok and results[index].ok)
            if answered:
                checkouts = spawn.parse_worktree_checkouts(results[index].stdout)
                self.machines[name] = {"fetched": fetched, "checkouts": checkouts,
                                       "home": results[0].stdout.strip(),
                                       "reach": machines.REACH[ANSWERED]}
            else:
                self.machines[name] = {"fetched": fetched, "checkouts": None, "home": None,
                                       "reach": row.get("reach") or machines.REACH[UNREACHED]}

    def locate(self, task_id):
        branch = spawn.branch_name(task_id)
        evidence = {}
        for name, seen in self.machines.items():
            here = branch in seen["fetched"] or branch in (seen["checkouts"] or {})
            evidence[name] = (here, seen["reach"])
        return machines.decide(self.project, task_id, branch in self.hub, evidence)

    def branch_checkout(self, task_id, name):
        """Where the task's branch is checked out on machine `name`, read
        from its project repository by this refresh; "unknown" when that
        machine did not answer -- never "none", which the board would
        show as a parked branch."""
        seen = self.machines[name]
        if seen["checkouts"] is None:
            return {"kind": "unknown", "path": None, "machine": name}
        path = seen["checkouts"].get(spawn.branch_name(task_id))
        machine = machines.machine_named(self.config, name)
        expected = remote.expand(remote.worktree_path(machine, self.project["name"], task_id), seen["home"])
        if path is None:
            return {"kind": "none", "path": None, "machine": name}
        if path.rstrip("/") == expected.rstrip("/"):
            return {"kind": "centrale", "path": path, "machine": name}
        return {"kind": "external", "path": f"{name}:{path}", "machine": name}


def board_placement(config, project, report):
    return BoardPlacement(config, project, report)
