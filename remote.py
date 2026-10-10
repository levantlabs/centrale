"""Life of a remote task (doc-3, task-207): spawn, resume, End session and
cleanup for a task placed on a machine (task-224: a project lists the
machines its tasks may run on, and each task's location is derived --
machines.locate), using that machine's project repository and worktree root; the task/* fetch back to the hub; and the
teardowns owed to a machine that could not be reached.

The hub keeps `main` and every claim: the claim commit is written on the
hub exactly as for a local spawn (spawn._claim_for_spawn), then `main` is
pushed to the worker's project repository (<repoRoot>/<project>/.git) and the
task worktree is cut from it there (<worktreeRoot>/<project>-<task>), so
nobody can edit the board on a worker. Every command on the worker goes
through machines.run_batch -- one round trip per step, POSIX-quoted, the
worker's own shell expanding `~` -- and every git call on the hub that
talks to a worker goes through run_hub_git, so tests fake both.

Results are three-way (machines.py). A step that never reached the
worker changed nothing there; a step whose outcome is UNKNOWN may have
run, so nothing is retried and nothing destructive follows from it. In
particular a launch whose answer never came back is reported as "outcome
unknown" and is never re-sent: tmux itself refuses a second session of
the same name, and the next Spawn's live-session check sees the first.

Only ever imported for a project with `machines`: with no machines
configured nothing here runs, so there is no ssh process, no fetch and no
owed-teardowns file (the single-machine guarantee).

Python 3.12 stdlib only. Calls into `server`, `spawn` and `machines` go
through the module objects at call time, like the rest of the codebase.
"""

from __future__ import annotations

import concurrent.futures
import inspect
import json
import os
import secrets
import shlex
import subprocess
import tempfile
import threading
import time

import codex_sessions
import machines
import server
import spawn
from machines import ANSWERED, UNKNOWN, UNREACHED, MachinePath

SpawnError = spawn.SpawnError

# The owed-teardowns record (doc-3, "A machine that cannot be reached").
OWED_ENV = "CENTRALE_OWED_TEARDOWNS"


# ---------------------------------------------------------------------------
# Where things are on a worker
# ---------------------------------------------------------------------------

def machine_for(config, project):
    """The machine dict a placed project's task runs on (machines.placed),
    or None for the hub."""
    return machines.machine_named(config, project.get("machine"))


def worktree_path(machine, project_name, task_id):
    """The task worktree on the worker, `~` left for its shell: the same
    <root>/<project>-<task> shape spawn.worktree_dir gives on the hub."""
    root = machine["worktreeRoot"].rstrip("/")
    return MachinePath(f"{root}/{project_name}-{task_id.lower()}")


_RULING_SCRIPT = r'''
wt=$1 branch=$2 task=$3 sender=$4 text=$5 backlog=$6
cd "$wt" || { echo "no Centrale worktree: $wt" >&2; exit 41; }
actual=$(git symbolic-ref --quiet --short HEAD) || actual=
[ "$actual" = "$branch" ] || { echo "$branch is not checked out in $wt" >&2; exit 41; }
set -- "backlog/tasks/$task - "*.md
[ "$#" -eq 1 ] && [ -f "$1" ] || { echo "no single task file for $task in $wt" >&2; exit 41; }
path=$1
status=$(git status --porcelain -- "$path") || exit 42
[ -z "$status" ] || { echo "$path has uncommitted edits in $wt" >&2; exit 41; }
"$backlog" task edit "$task" --comment "$text" --comment-author "$sender" || exit 42
git add -- "$path" || exit 42
git commit --only -m "backlog: ruling on $task from $sender" -- "$path" || exit 42
sha=$(git rev-parse HEAD) || exit 42
printf 'centrale-ruling-path %s\ncentrale-ruling-commit %s\n' "$path" "$sha"
'''


def commit_ruling(config, project, task_id, sender, text):
    """Write a no-agent ruling in the worker's task worktree (task-230)."""
    machine = machine_for(config, project)
    branch = spawn.branch_name(task_id)
    wt = worktree_path(machine, project["name"], task_id)
    backlog = resolve_executable(config, machine, "backlog")
    result = machines.run_on_machine(config, machine["name"], _sh(
        _RULING_SCRIPT, wt, branch, task_id.lower(), sender.strip(), text, backlog))
    if result.status != ANSWERED:
        raise SpawnError(f"machine {machine['name']}: ruling outcome unknown: {result.stderr or result.status}",
                         status=503)
    if result.returncode != 0:
        raise SpawnError(result.stderr.strip() or result.stdout.strip() or "worker ruling failed",
                         status=409 if result.returncode == 41 else 502)
    path = _marker(result.stdout, "centrale-ruling-path")
    commit = _marker(result.stdout, "centrale-ruling-commit")
    if not path or not commit:
        raise SpawnError("worker ruling ran but its commit could not be confirmed", status=502)
    return {"branch": branch, "worktree": str(wt), "path": path, "commit": commit}


def expand(path, home):
    """A worker path as the worker's own git prints it: `~` replaced by
    the home its shell reported, so it can be compared with git output."""
    if path == "~":
        return home
    if path.startswith("~/"):
        return f"{home.rstrip('/')}/{path[2:]}"
    return str(path)


def tmux_argv(machine):
    """`tmux` on the worker, with its -L socket when the machine names one
    (machines.tmux_argv, task-209; the default server otherwise)."""
    return machines.tmux_argv(machine)


def attach_command(machine, name):
    return shlex.join(["ssh", "-t", machine["ssh"], *tmux_argv(machine), "attach", "-t", name])


def git_url(machine, path):
    """A git URL for a path on the worker, over the same ssh target. In
    scp-like syntax a relative path is relative to the login home, which
    is how `~/` is said there without the hub ever expanding it."""
    target = machine["ssh"]
    path = str(path)
    if target.startswith("ssh://"):
        if path == "~" or path.startswith("~/"):
            return f"{target.rstrip('/')}/~/{path[2:]}"
        return f"{target.rstrip('/')}{path}"
    if path == "~":
        return f"{target}:."
    if path.startswith("~/"):
        return f"{target}:{path[2:]}"
    return f"{target}:{path}"


def tracking_ref(machine_name, task_id):
    """Where the hub keeps its fetched copy of a worker's task branch:
    under refs/remotes, never refs/heads, so an amend on the worker can
    never break a fetch (doc-3, "Running")."""
    return f"refs/remotes/{machine_name}/{spawn.branch_name(task_id)}"


def run_hub_git(args, cwd, timeout=None):
    """git on the hub that talks to a worker (push, fetch): the subprocess
    boundary tests patch. GIT_SSH_COMMAND makes git ride the kept-open
    ssh master instead of opening a fresh connection each time."""
    try:
        env = machines.git_ssh_env()
    except OSError as exc:
        return subprocess.CompletedProcess(["git", *args], 255, "", str(exc))
    return server._run(["git", *args], cwd=cwd, env=env,
                       timeout=timeout or server._subprocess_timeout_seconds)


def _reason(result):
    return (result.stderr or result.stdout or "").strip() or result.status


def _step_error(machine_name, step, result, status=None):
    """The SpawnError for a step that did not answer cleanly: it names the
    machine and the step (doc-3), and says whether anything may have run."""
    if result.status == UNREACHED:
        return SpawnError(f"machine {machine_name} is unreachable ({step}): {_reason(result)}", status=503)
    if result.status == UNKNOWN:
        return SpawnError(
            f"machine {machine_name}: outcome unknown at step '{step}' -- the command may or may "
            f"not have run there ({_reason(result)})", status=504)
    return SpawnError(f"machine {machine_name}: {step} failed: {_reason(result)}", status=status or 502)


# ---------------------------------------------------------------------------
# Scripts the worker runs (sent as argv; nothing is installed there)
# ---------------------------------------------------------------------------

_SESSIONS_SCRIPT = r'''
out=$("$@" list-sessions -F '#{session_name}	#{session_created}' 2>&1); r=$?
if [ $r -ne 0 ]; then
  case "$out" in
    *"no server running"*|*"error connecting"*|*"No such file or directory"*) exit 0 ;;
  esac
  printf '%s\n' "$out" >&2; exit $r
fi
printf '%s\n' "$out"
'''

_HOME_SCRIPT = 'printf "%s" "$HOME"'

# $1 bare repo, $2 worktree, $3 branch, $4 base, then (relative, source)
# pairs for the project's worktreeLinks. Prints "centrale-worktree
# reused|created|created-branch", and one "centrale-link-warning ..." per
# link it skipped. A link is made only once git ignores its destination,
# with the same refusals as spawn._link_worktree_paths on the hub.
_WORKTREE_SCRIPT = r'''
bare=$1 wt=$2 branch=$3 base=$4; shift 4
if [ -d "$wt" ]; then
  common=$(git -C "$wt" rev-parse --path-format=absolute --git-common-dir 2>/dev/null) || common=
  if [ -n "$common" ] && [ "$(cd "$common" && pwd -P)" = "$(cd "$bare" && pwd -P)" ]; then
    echo "centrale-worktree reused"; exit 0
  fi
fi
mkdir -p "$(dirname "$wt")" || exit 10
if git --git-dir="$bare" rev-parse --verify -q "refs/heads/$branch" >/dev/null; then
  git --git-dir="$bare" worktree add "$wt" "$branch" >&2 || exit 11
  echo "centrale-worktree created"
else
  git --git-dir="$bare" worktree add -b "$branch" "$wt" "$base" >&2 || exit 11
  echo "centrale-worktree created-branch"
fi
exclude=$(git -C "$wt" rev-parse --path-format=absolute --git-path info/exclude) || exclude=
while [ $# -ge 2 ]; do
  rel=$1 src=$2; shift 2
  if [ ! -e "$src" ]; then echo "centrale-link-warning $rel: source path is missing: $src"; continue; fi
  tracked=$(git -C "$wt" ls-files -- ":(literal)$rel") || tracked=error
  if [ -n "$tracked" ]; then
    echo "centrale-link-warning $rel: destination is tracked or git could not verify it is untracked"; continue
  fi
  parent=$wt; unsafe=; rest=${rel%/*}; [ "$rest" = "$rel" ] && rest=
  old_ifs=$IFS; IFS=/
  for part in $rest; do parent="$parent/$part"; [ -L "$parent" ] && { unsafe=$parent; break; }; done
  IFS=$old_ifs
  if [ -n "$unsafe" ]; then echo "centrale-link-warning $rel: destination parent is a symlink: $unsafe"; continue; fi
  dest="$wt/$rel"
  if [ -e "$dest" ] || [ -L "$dest" ]; then echo "centrale-link-warning $rel: destination already exists: $dest"; continue; fi
  if [ -z "$exclude" ]; then echo "centrale-link-warning $rel: could not locate git info/exclude"; continue; fi
  if ! grep -qxF "/$rel" "$exclude" 2>/dev/null; then
    mkdir -p "$(dirname "$exclude")"
    if [ -s "$exclude" ] && [ -n "$(tail -c 1 "$exclude")" ]; then echo >> "$exclude"; fi
    printf '%s\n' "/$rel" >> "$exclude"
  fi
  if ! git -C "$wt" check-ignore -q -- "$rel"; then
    echo "centrale-link-warning $rel: git info/exclude is overridden or could not be verified"; continue
  fi
  mkdir -p "$(dirname "$dest")" && ln -s "$src" "$dest" || echo "centrale-link-warning $rel: could not create the link"
done
exit 0
'''

# Undo a worktree this spawn created when the launch definitely failed:
# $1 bare, $2 worktree, $3 branch, $4 "branch" when the branch was new too.
_ROLLBACK_SCRIPT = r'''
git --git-dir="$1" worktree remove --force "$2" || exit 1
if [ "$4" = branch ]; then git --git-dir="$1" branch -D "$3" >/dev/null || exit 2; fi
'''

# The orphan sweep (task-201, doc-3 "End session"), as a python3 program
# the worker runs with `-c`: every pid whose cwd is under the worktree --
# nohup and setsid children survive kill-session -- except Codex's shared
# per-user app-server daemon, recognised by its executable's directory and
# never by its cwd (task-222; server._is_codex_shared_daemon on the hub).
# argv: <worktree> list|kill. Prints the pids, space-separated.
SWEEP_PROGRAM = r'''
import os, signal, sys
target = os.path.realpath(sys.argv[1])
prefix = target.rstrip("/") + "/"
me = os.getpid()
found = []
for entry in os.listdir("/proc"):
    if not entry.isdigit() or int(entry) == me:
        continue
    try:
        cwd = os.readlink("/proc/%s/cwd" % entry)
    except OSError:
        continue
    if cwd.endswith(" (deleted)"):
        cwd = cwd[:-len(" (deleted)")]
    if cwd != target and not cwd.startswith(prefix):
        continue
    try:
        exe = os.readlink("/proc/%s/exe" % entry)
    except OSError:
        exe = ""
    if "/packages/app-server-daemon/" in exe:
        continue
    found.append(int(entry))
found.sort()
if sys.argv[2] == "kill":
    for pid in found:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
print(" ".join(str(pid) for pid in found))
'''

# End a task session and sweep its worktree. $1 session, $2 latest
# acceptable session_created ("" for any), $3 worktree, $4 strict|lenient
# (strict: no session is an error, as for a click), $5 the sweep program,
# then the tmux argv. Linked windows (the control session, task-209) share
# the task session's window ids and outlive kill-session, so each is
# killed by id afterwards.
_END_SCRIPT = r'''
name=$1 notafter=$2 wt=$3 mode=$4 sweep=$5; shift 5
created=$("$@" display-message -p -t "=$name:" '#{session_created}' 2>/dev/null) || created=
if [ -z "$created" ]; then
  if [ "$mode" = strict ]; then echo "centrale-no-session"; exit 3; fi
  echo "centrale-session-gone"
else
  if [ -n "$notafter" ] && [ "$created" -gt "$notafter" ]; then echo "centrale-newer $created"; exit 5; fi
  ids=$("$@" list-windows -t "=$name" -F '#{window_id}' 2>/dev/null)
  "$@" kill-session -t "=$name" || exit 4
  for id in $ids; do "$@" kill-window -t "$id" 2>/dev/null; done
  echo "centrale-ended $ids"
fi
pids=$(python3 -c "$sweep" "$wt" kill) || { echo "centrale-sweep-failed"; exit 6; }
echo "centrale-swept $pids"
'''

# Remove a merged task's worktree and branch from the project repository.
# $1 bare, $2 worktree, $3 branch, $4 the tip the hub verified as merged,
# $5 1 to kill leftover processes, $6 the sweep program.
_CLEANUP_SCRIPT = r'''
bare=$1 wt=$2 branch=$3 expected=$4 force=$5 sweep=$6
tip=$(git --git-dir="$bare" rev-parse -q --verify "refs/heads/$branch") || tip=
if [ -n "$tip" ] && [ "$tip" != "$expected" ]; then echo "centrale-moved $tip"; exit 7; fi
if [ -d "$wt" ]; then
  status=$(git -C "$wt" status --porcelain) || exit 9
  echo "centrale-status-begin"
  if [ -n "$status" ]; then printf '%s\n' "$status"; fi
  echo "centrale-status-end"
  if [ -n "$status" ] && [ "$force" != 1 ]; then exit 12; fi
  pids=$(python3 -c "$sweep" "$wt" list) || { echo "centrale-sweep-failed"; exit 6; }
  if [ -n "$pids" ] && [ "$force" != 1 ]; then echo "centrale-busy $pids"; exit 8; fi
  if [ -n "$pids" ]; then python3 -c "$sweep" "$wt" kill >/dev/null; fi
  echo "centrale-killed $pids"
  git --git-dir="$bare" worktree remove --force "$wt" || exit 10
  echo "centrale-removed"
fi
git --git-dir="$bare" worktree prune
if [ -n "$tip" ]; then git --git-dir="$bare" branch -D "$branch" >/dev/null || exit 11; echo "centrale-branch-deleted"; fi
exit 0
'''


def _sh(script, *args):
    return ["sh", "-c", script, "sh", *args]


def _codex_lookup_program():
    return inspect.getsource(codex_sessions)


def _marker(stdout, word):
    """The rest of the first line starting with `word`, or None."""
    for line in (stdout or "").splitlines():
        if line == word or line.startswith(word + " "):
            return line[len(word):].strip()
    return None


# ---------------------------------------------------------------------------
# Reading the worker's state
# ---------------------------------------------------------------------------

def _parse_sessions(stdout):
    sessions = []
    for line in (stdout or "").splitlines():
        parts = line.strip().split("\t")
        if len(parts) >= 2 and parts[0].startswith(server.SESSION_PREFIX):
            sessions.append({"name": parts[0], "created": parts[1], "attached": False})
    return sessions


def survey(config, machine, project, task_id):
    """One round trip: the worker's centrale-* sessions, its $HOME and the
    project's checkouts in the project repository. Returns (sessions, home,
    {branch: path}); raises SpawnError when the worker did not answer."""
    name = machine["name"]
    bare = machines.repo_git_dir_path(machine, project)
    results = machines.run_batch(config, name, [
        (_sh(_SESSIONS_SCRIPT, *tmux_argv(machine)), None),
        (_sh(_HOME_SCRIPT), None),
        (["sh", "-c", machines._WORKTREE_LIST, "sh", bare], None),
    ])
    for step, result in zip(("list sessions", "read home", "list worktrees"), results):
        if not result.ok:
            raise _step_error(name, step, result)
    return (_parse_sessions(results[0].stdout), results[1].stdout.strip(),
            spawn.parse_worktree_checkouts(results[2].stdout))


def sessions_elsewhere(config, project, machine_name):
    """The live centrale-* sessions of every place `project` may run that
    is not `machine_name` (None: the hub): the hub's tmux unless that is
    the place asked about, and each of the project's other machines in
    one round trip apiece, in parallel. A project's maxAgents counts its
    agents on every machine (doc-3); a machine that does not answer adds
    nothing it cannot show."""
    found = []
    if machine_name is not None:
        try:
            found += server.list_sessions()
        except server.BacklogError as exc:
            raise SpawnError(f"failed to check existing sessions on the hub: {exc}", status=502) from exc
    others = [m for m in project.get("machines") or [] if m != machine_name]
    answers = machines.run_on_machines(config, {
        m: [(_sh(_SESSIONS_SCRIPT, *tmux_argv(machines.machine_named(config, m))), None)] for m in others
    }, timeout=machines.refresh_budget(config))
    for m in others:
        result = (answers.get(m) or [None])[0]
        if result is not None and result.ok:
            found += _parse_sessions(result.stdout)
    return found


def _check_capacity(config, machine, project, name, sessions):
    if name in {s["name"] for s in sessions}:
        raise SpawnError(f"session already exists on machine {machine['name']}: {name}", status=409)
    cap = project.get("maxAgents")
    if cap is not None:
        everywhere = sessions + sessions_elsewhere(config, project, machine["name"])
        live = server.live_sessions_for_project(project["name"], config, sessions=everywhere)
        if len(live) >= cap:
            raise SpawnError(
                f"Project {project['name']} has {len(live)} live agent sessions across its machines "
                f"(maxAgents: {cap}): {', '.join(sorted(live))}. "
                "End a session before launching another agent.", status=409)
    # doc-3: maxAgents applies per machine as well as per project.
    cap = machine.get("maxAgents")
    if cap is not None:
        here = {p["name"] for p in machines.projects_on(config, machine["name"])}
        owners = {s["name"]: server._parse_session_project_and_task(s["name"], config)[0] for s in sessions}
        live = sorted(name for name, owner in owners.items() if owner and owner.get("name") in here)
        if len(live) >= cap:
            raise SpawnError(
                f"Machine {machine['name']} has {len(live)} live agent sessions (maxAgents: {cap}): "
                f"{', '.join(live)}. End a session before launching another agent.", status=409)


def _check_external(machine, project, task_id, home, checkouts):
    branch = spawn.branch_name(task_id)
    path = checkouts.get(branch)
    expected = expand(worktree_path(machine, project["name"], task_id), home)
    if path is not None and path.rstrip("/") != expected.rstrip("/"):
        raise SpawnError(spawn.external_checkout_reason(branch, f"{machine['name']}:{path}"), status=409)


def push_base(config, machine, project):
    """Push the hub's base branch to the worker's project repository, so the
    worktree is cut from exactly the claim just committed here. Not
    forced: the worker's copy only ever moves forward from the hub's."""
    base = spawn.current_branch(project["path"])
    if base == "HEAD":
        raise SpawnError(f"{project['path']} has a detached HEAD: no base branch to push to "
                         f"machine {machine['name']}", status=409)
    url = git_url(machine, machines.repo_git_dir_path(machine, project))
    proc = run_hub_git(["push", "--quiet", url, f"refs/heads/{base}:refs/heads/{base}"], project["path"])
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "git push failed").strip()
        raise SpawnError(f"machine {machine['name']}: pushing {base} to its project repository failed: "
                         f"{detail}", status=502)
    return base


# task-240: a fresh check tests the tree the worker will receive, before claim.
WORKER_CHECK_SECONDS = 10
WORKER_CHECK_GRACE_SECONDS = 2
WORKER_CHECK_OUTPUT_LIMIT = 2000

# task-240: checks may leave TERM-ignoring/setsid children. Bound the
# sweep separately from command timeout and verify before deleting the tree.
# Existing harvest/session sweep semantics remain unchanged.
_WORKER_SWEEP_PROGRAM = r'''
import os, signal, sys, time
# task-240: non-Linux workers still remove scratch trees without /proc.
if not os.path.isdir("/proc"):
    sys.exit(0)
target = os.path.realpath(sys.argv[1])
prefix = target.rstrip("/") + "/"

def descendants():
    found = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit() or int(entry) == os.getpid():
            continue
        try:
            cwd = os.readlink("/proc/%s/cwd" % entry).removesuffix(" (deleted)")
        except OSError:
            continue
        if cwd == target or cwd.startswith(prefix):
            found.append(int(entry))
    return found

def send(pids, sig):
    for pid in pids:
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            pass

send(descendants(), signal.SIGTERM)
deadline = time.monotonic() + 1
while descendants() and time.monotonic() < deadline:
    time.sleep(0.05)
deadline = time.monotonic() + 1
while descendants() and time.monotonic() < deadline:
    send(descendants(), signal.SIGKILL)
    time.sleep(0.05)
sys.exit(1 if descendants() else 0)
'''

# TMPDIR is the worker's overridable scratch root, as for remote harvest.
# Reuse the link safety rules verbatim; never create a task branch here.
_WORKER_CHECK_SCRIPT = r'''bare=$1 sha=$2 limit=$3 grace=$4 sweep=$5 command=$6 executable=$7; shift 7
scratch=$(mktemp -d "${TMPDIR:-/tmp}/centrale-worker-check-XXXXXX") || exit 22
log=$scratch.output
cleanup() {
  rc=$?
  trap - EXIT
  cd /
  clean=0
  python3 -c "$sweep" "$scratch" kill >/dev/null 2>&1 || clean=1
  if [ -f "$scratch/.git" ]; then
    git --git-dir="$bare" worktree remove --force "$scratch" >/dev/null 2>&1 || clean=1
  fi
  rm -rf "$scratch" "$log" || clean=1
  git --git-dir="$bare" worktree prune >/dev/null 2>&1 || clean=1
  [ "$clean" = 0 ] || { echo "scratch cleanup failed" >&2; exit 30; }
  exit "$rc"
}
trap cleanup EXIT
rmdir "$scratch" || exit 22
git --git-dir="$bare" worktree add --detach "$scratch" "$sha" >"$log" 2>&1 || { tail -c 2000 "$log"; exit 22; }
wt=$scratch
''' + ('exclude=$(git' + _WORKTREE_SCRIPT.split('exclude=$(git', 1)[1].rsplit('exit 0', 1)[0]) + r'''
cd "$scratch" || exit 22
# Distinguish a missing check from a present check reporting a missing tool.
[ -f "$executable" ] && [ -x "$executable" ] || exit 31
timeout -k "$grace" "$limit" sh -c "$command" </dev/null >"$log" 2>&1
rc=$?
tail -c 2000 "$log"
exit "$rc"
'''


def check_worker_readiness(config, machine, project, base_sha):
    """Return (passed, reason); output is bounded and no result is retained."""
    check = project["workerCheck"]
    argv = shlex.split(check["command"])
    argv[0] = "./" + argv[0]
    command = shlex.join(argv)
    links = []
    for rel in project.get("worktreeLinks") or []:
        links += [rel, machines.links_source_path(machine, project, rel)]
    result = machines.run_on_machine(config, machine["name"], _sh(
        _WORKER_CHECK_SCRIPT, machines.repo_git_dir_path(machine, project), base_sha,
        str(WORKER_CHECK_SECONDS), str(WORKER_CHECK_GRACE_SECONDS), _WORKER_SWEEP_PROGRAM,
        command, argv[0], *links), timeout=WORKER_CHECK_SECONDS + WORKER_CHECK_GRACE_SECONDS + 20)
    output = (result.stdout + "\n" + result.stderr).strip()[-WORKER_CHECK_OUTPUT_LIMIT:]
    if result.status != ANSWERED:
        return False, f"worker check outcome {result.status}: {output}"
    rc = result.returncode
    if rc == 0:
        return True, "worker check passed"
    if rc in (124, 137):
        reason = f"worker check timed out after {WORKER_CHECK_SECONDS}s"
    elif rc == 31:
        reason = f"setup not finished: check command {check['command']} missing or not executable"
    elif rc == 30:
        reason = "worker check scratch cleanup failed"
    else:
        reason = f"worker check failed (exit {rc})"
    return False, f"{reason}: {output or 'no output'}"


def prepare_worker_readiness(config, machine, project):
    """Only spawn pushes: pin the pushed base without committing a claim."""
    base = spawn.current_branch(project["path"])
    pin = server.run_git(["rev-parse", "--verify", f"refs/heads/{base}"], project["path"])
    sha = pin.stdout.strip()
    if base == "HEAD" or pin.returncode or not sha:
        return False, "cannot pin the hub base for worker check"
    pushed = run_hub_git(["push", "--quiet", git_url(machine, machines.repo_git_dir_path(machine, project)),
                          f"{sha}:refs/heads/{base}"], project["path"])
    if pushed.returncode:
        return False, f"pushing base for worker check failed: {_reason(pushed)[:2000]}"
    return check_worker_readiness(config, machine, project, sha)


def doctor_worker_readiness(config, machine, project):
    """Read the parked base; doctor never pushes or claims (reviewer ruling)."""
    prefix = f"machine {machine['name']}: {project['name']}: "
    base = spawn.current_branch(project["path"])
    result = machines.run_on_machine(config, machine["name"], [
        "git", "--git-dir", machines.repo_git_dir_path(machine, project),
        "rev-parse", "--verify", f"refs/heads/{base}"])
    if result.status != ANSWERED:
        return "FAIL", prefix + f"cannot read worker {base}: {_reason(result)[:2000]}"
    if result.returncode != 0 or not result.stdout.strip():
        return "WARN", prefix + "not pushed yet; checked at the next spawn"
    sha = result.stdout.strip()
    hub = server.run_git(["rev-parse", "--verify", f"refs/heads/{base}"], project["path"])
    behind = ""
    if hub.returncode == 0 and hub.stdout.strip() != sha:
        ancestor = server.run_git(["merge-base", "--is-ancestor", sha, f"refs/heads/{base}"], project["path"])
        state = f"behind hub {base}" if ancestor.returncode == 0 else f"differs from hub {base}"
        behind = f" (worker {base} {state}; checked again at the next spawn)"
    passed, reason = check_worker_readiness(config, machine, project, sha)
    return ("PASS" if passed else "FAIL"), prefix + reason + behind


def ensure_worktree(config, machine, project, task_id, base, warnings):
    """Create (or reuse) the task worktree on the worker, with the
    project's worktreeLinks. Returns (path, created) where created is
    None (reused), "worktree" or "branch" (both new)."""
    wt = worktree_path(machine, project["name"], task_id)
    links = []
    for rel in project.get("worktreeLinks") or []:
        links += [rel, machines.links_source_path(machine, project, rel)]
    result = machines.run_on_machine(config, machine["name"], _sh(
        _WORKTREE_SCRIPT, machines.repo_git_dir_path(machine, project), wt,
        spawn.branch_name(task_id), base, *links))
    if not result.ok:
        raise _step_error(machine["name"], "create the task worktree", result, status=500)
    state = _marker(result.stdout, "centrale-worktree")
    for line in result.stdout.splitlines():
        if line.startswith("centrale-link-warning "):
            warnings.append(f"machine {machine['name']}: worktreeLinks "
                            f"{line[len('centrale-link-warning '):]} -- skipped link")
    created = {"reused": None, "created": "worktree", "created-branch": "branch"}.get(state)
    return wt, created


def _rollback(config, machine, project, task_id, wt, created):
    if not created:
        return None
    result = machines.run_on_machine(config, machine["name"], _sh(
        _ROLLBACK_SCRIPT, machines.repo_git_dir_path(machine, project), wt,
        spawn.branch_name(task_id), created))
    return None if result.ok else f"rolling back the worktree failed too: {_reason(result)}"


def resolve_executable(config, machine, executable):
    """An agent CLI's full path on the worker, found the way the owner's
    own terminal finds it (machines._resolve_probe: an interactive shell,
    since ssh's PATH lacks a per-user npm bin). Asked on every launch
    rather than remembered: a reinstalled CLI moves."""
    result = machines.run_on_machine(config, machine["name"], machines._resolve_probe(executable))
    if result.status != ANSWERED:
        raise _step_error(machine["name"], f"find {executable}", result)
    path = machines._resolved_path(result.stdout) if result.returncode == 0 else None
    if path is None:
        raise SpawnError(f"machine {machine['name']}: {executable} not found by an interactive shell "
                         f"there (bash -ic 'command -v {executable}')", status=502)
    return path


def _session_path(config, machine):
    """Give a new worker session its login PATH plus interactive tool dirs.

    The ssh command's shell has the worker's login PATH, while user npm
    directories may only be added by interactive startup files. Resolve
    those tools afresh for each launch so a moved installation is found.
    """
    tools = ("backlog", "claude", "codex")
    commands = [(["sh", "-c", 'printf "%s\\n" "$PATH"'], None)]
    commands.extend((machines._resolve_probe(tool), None) for tool in tools)
    results = machines.run_batch(config, machine["name"], commands)
    login = results[0]
    if not login.ok or not login.stdout.strip():
        raise _step_error(machine["name"], "read the login PATH", login)
    paths = login.stdout.strip().split(":")
    for tool, result in zip(tools, results[1:]):
        if result.status != ANSWERED:
            raise _step_error(machine["name"], f"find {tool}", result)
        path = machines._resolved_path(result.stdout) if result.returncode == 0 else None
        if path is None:
            if tool == "backlog":
                raise SpawnError(f"machine {machine['name']}: backlog not found by an interactive shell "
                                 "there (bash -ic 'command -v backlog')", status=502)
            continue  # A worker need only install the agent selected for this task.
        directory = os.path.dirname(path)
        if directory not in paths:
            paths.append(directory)
    return ":".join(paths)


def probe_codex_hook_trust(config, machine, path):
    """server.probe_codex_hook_trust against the WORKER's codex: whether
    its --help advertises --dangerously-bypass-hook-trust. No answer reads
    as no, like the hub's probe."""
    result = machines.run_on_machine(config, machine["name"], [path, "--help"], timeout=10)
    return result.status == ANSWERED and "dangerously-bypass-hook-trust" in (result.stdout + result.stderr)


def _agent_argv(config, machine, cmd, agent_name):
    """The launch argv on the worker: argv[0] by its full path there, plus
    the status hooks for that machine's sink. Under CENTRALE_SPAWN_CMD
    (agent_name None) the override runs as given, with no hooks."""
    if agent_name is None:
        return list(cmd)
    executable = cmd[0]
    path = executable if executable.startswith("/") else resolve_executable(config, machine, executable)
    resolved = [path, *cmd[1:]]
    if spawn.agent_kind_for_command(resolved) == "codex":
        supports = probe_codex_hook_trust(config, machine, path)
        return [*resolved, *_worker_hooks(resolved, supports_hooks=supports)]
    return [*resolved, *_worker_hooks(resolved)]


def _worker_hooks(cmd, supports_hooks=None):
    """The hook/notify argv for an agent on a worker. Workers cannot
    reach Centrale's API, and the hub's notify script does not exist
    there (a hook command that cannot start exits 2, which BLOCKS a Claude
    tool call), so the hooks use the tmux pane-option sink (task-209)."""
    return spawn._inject_agent_hooks(cmd, sink="tmux", codex_supports_hooks=supports_hooks)


def _launch(config, machine, name, wt, env, argv, spawn_id, identity=()):
    """new-session plus everything that must happen to the same session,
    in ONE tmux invocation: the spawn id, the held geometry and the
    identity pane options (task-209, when present)."""
    columns, rows = spawn.SESSION_GEOMETRY
    tmux_args = spawn.new_session_args(name, wt)
    for key, value in env:
        tmux_args += ["-e", f"{key}={value}"]
    # task-232: tmux records -e PATH in the session but its first window
    # still starts with the server's old PATH (reproduced on a worker).
    # Pass it to the initial agent process as well as the session.
    session_path = next((value for key, value in env if key == "PATH"), None)
    tmux_args += ["env", f"PATH={session_path}", *argv] if session_path else argv
    tmux_args += [";", "set-option", "-t", f"={name}:", "@centrale_spawn", spawn_id]
    tmux_args += [";", "set-option", "-t", f"={name}:", "window-size", "manual"]
    tmux_args += [";", "resize-window", "-t", f"={name}:", "-x", str(columns), "-y", str(rows)]
    for command in identity:
        tmux_args += [";", *(command[1:] if command[:1] == ["tmux"] else command)]
    return machines.run_on_machine(config, machine["name"], [*tmux_argv(machine), *tmux_args])


def _identity_commands(name, project, task_id, kind):
    """task-209's identity pane options for the new session, set in the
    SAME tmux call as new-session so its session scan sees them; none
    while remote_status is not present."""
    try:
        import remote_status  # task-209
    except ImportError:
        return []
    return [list(c) for c in remote_status.identity_commands(f"={name}:", project["name"], task_id,
                                                             kind or "custom")]


def _rearm(config, machine):
    """task-209's control-session link and subscriptions, best-effort:
    the agent is already running when this is called."""
    try:
        import remote_status  # task-209
        remote_status.rearm(config, machine["name"])
    except Exception:  # never fail a launch that already happened
        pass


def capture_pane(config, machine, name):
    """The worker session's current screen (spawn.codex_resume_readiness's
    capture), raising server.PaneCaptureError like the hub's capture."""
    result = machines.run_on_machine(config, machine["name"],
                                     [*tmux_argv(machine), "capture-pane", "-p", "-t", f"={name}:"])
    if result.status != ANSWERED:
        raise server.PaneCaptureError(f"machine {machine['name']}: {_reason(result)}", status=503)
    if result.returncode != 0:
        raise server.PaneCaptureError(f"no live session {name} on machine {machine['name']}", status=404)
    lines = result.stdout.split("\n")
    while lines and not lines[-1].strip():
        lines.pop()
    return lines


def codex_lookup(config, machine, wt):
    """codex_sessions on the worker: (its Codex home, verified UUID or
    None) for this worktree, read from the WORKER's ~/.codex/sessions."""
    result = machines.run_on_machine(config, machine["name"],
                                     ["python3", "-c", _codex_lookup_program(), wt])
    if not result.ok:
        raise _step_error(machine["name"], "look up the Codex conversation", result)
    lines = result.stdout.split("\n")
    home = lines[0].strip() if lines else ""
    identity = lines[1].strip() if len(lines) > 1 else ""
    if not home:
        raise SpawnError(f"machine {machine['name']}: the Codex conversation lookup printed nothing",
                         status=502)
    return home, identity or None


# ---------------------------------------------------------------------------
# Spawn and resume
# ---------------------------------------------------------------------------

def _start(config, machine, project, task_id, name, wt, created, launch_env, argv,
           agent_name, agent_kind, warnings):
    """Launch, then everything after: the outcome is decided here."""
    spawn_id = secrets.token_hex(8)
    env = [*launch_env, ("CENTRALE_SPAWN_ID", spawn_id)]
    if agent_name is not None:
        env.append(("CENTRALE_AGENT", agent_name))
    server.clear_agent_event(project["name"], task_id, agent_name=agent_name or "custom")
    launched_at = time.time()
    result = _launch(config, machine, name, wt, env, argv, spawn_id,
                     _identity_commands(name, project, task_id, agent_kind))
    if result.status == UNKNOWN:
        # Never retried and never rolled back: the session may be running.
        raise SpawnError(
            f"machine {machine['name']}: outcome unknown -- the connection ended before the worker "
            f"confirmed session {name} (spawn id {spawn_id}). It may be running: check before "
            f"retrying; a retry refuses while it is live. ({_reason(result)})", status=504)
    if not result.ok:
        undo = _rollback(config, machine, project, task_id, wt, created) \
            if result.status == ANSWERED else None
        error = _step_error(machine["name"], "start the agent session", result, status=500)
        if undo:
            error = SpawnError(f"{error.message}; {undo}", status=error.status)
        raise error
    server.record_session_started(project["name"], task_id, agent_name or "custom", launched_at,
                                  machine=machine["name"])
    _rearm(config, machine)
    return spawn_id


def _result(machine, name, agent_name, wt, spawn_id, warnings):
    result = {
        "session": name,
        "attach": attach_command(machine, name),
        "agent": agent_name if agent_name is not None else "custom",
        "machine": machine["name"],
        "worktree": str(wt),
        "spawnId": spawn_id,
    }
    if warnings:
        result["warnings"] = warnings
    return result


def spawn_on_machine(config, project_name, task_id, agent=None, machine_name=None,
                     first_message=None):
    """spawn.spawn() for a task placed on machine `machine_name` (doc-3
    "Life of a remote task: Spawn"), under the same per-project launch lock. Owed teardowns
    for the task settle first; then the worker's sessions, home and
    checkouts in one round trip (409s as on the hub, maxAgents per
    project and per machine); the Done check and agent choice; the claim
    committed on the HUB's main; main pushed to the project repository; the
    worktree with its links; the agent by its full path there."""
    project = machines.placed(spawn._validate_request(config, project_name, task_id), machine_name)
    machine = machine_for(config, project)
    requested = spawn.validate_requested_agent(config, agent)
    name = spawn.session_name(project_name, task_id)
    settle_owed(config, machine["name"], project_name=project_name, task_id=task_id,
                holding_task_lock=True)
    sessions, home, checkouts = survey(config, machine, project, task_id)
    _check_capacity(config, machine, project, name, sessions)

    task = None if spawn.spawn_cmd_override() else spawn._check_not_done(project, task_id)
    _check_external(machine, project, task_id, home, checkouts)
    agent_name, cmd, prompt_suffix, agent_kind, recorded, choice_warning = (
        spawn._choose_spawn_agent(config, task_id, task, requested))
    if project.get("workerCheck"):
        passed, reason = prepare_worker_readiness(config, machine, project)
        if not passed:
            check = project["workerCheck"]
            raise SpawnError(f"machine {machine['name']}: {reason}; read {check['doc']} in "
                             f"{project_name} and set up {machine['name']}, or spawn on the hub", status=409)
    warnings = spawn._claim_for_spawn(config, project, task_id, recorded, choice_warning)

    base = push_base(config, machine, project)
    wt, created = ensure_worktree(config, machine, project, task_id, base, warnings)
    try:
        argv = _agent_argv(config, machine, cmd, agent_name)
        session_path = _session_path(config, machine)
    except SpawnError as exc:
        undo = _rollback(config, machine, project, task_id, wt, created)
        raise SpawnError(f"{exc.message}; {undo}" if undo else exc.message, status=exc.status) from exc
    prompt = spawn.initial_prompt(task_id, config, prompt_suffix, first_message)
    spawn_id = _start(config, machine, project, task_id, name, wt, created,
                      [("PATH", session_path)], [*argv, prompt],
                      agent_name, agent_kind, warnings)
    return _result(machine, name, agent_name, wt, spawn_id, warnings)


def resume_on_machine(config, project_name, task_id, reconcile=False, machine_name=None):
    """spawn.resume() for a task placed on machine `machine_name`: the same tiers
    (spawn._resume_launch), with Codex's verified conversation read from
    the worker's own ~/.codex/sessions and its readiness read from the
    worker's pane."""
    project = machines.placed(spawn._validate_request(config, project_name, task_id), machine_name)
    machine = machine_for(config, project)
    name = spawn.session_name(project_name, task_id)
    settle_owed(config, machine["name"], project_name=project_name, task_id=task_id,
                holding_task_lock=True)
    sessions, home, checkouts = survey(config, machine, project, task_id)
    _check_capacity(config, machine, project, name, sessions)

    task = None if spawn.spawn_cmd_override() else spawn._check_not_done(project, task_id)
    _check_external(machine, project, task_id, home, checkouts)
    warnings = []
    base = push_base(config, machine, project)
    wt, created = ensure_worktree(config, machine, project, task_id, base, warnings)

    launch = spawn._resume_launch(config, project, task_id, task, reconcile, wt,
                                  lambda: codex_lookup(config, machine, wt))
    warnings.extend(launch["warnings"])
    agent_name, agent_kind = launch["agent_name"], launch["agent_kind"]
    argv = _agent_argv(config, machine, launch["cmd"], agent_name)
    if launch["prompt_arg"] is not None:
        argv.append(launch["prompt_arg"])
    env = [("PATH", _session_path(config, machine))]
    if launch["codex_launch"]:
        env.append(("CODEX_HOME", launch["codex_home"]))
    spawn_id = _start(config, machine, project, task_id, name, wt, created, env, argv,
                      agent_name, agent_kind, warnings)

    result = _result(machine, name, agent_name, wt, spawn_id, None)
    result["resumed"] = True
    if launch["codex_launch"]:
        readiness, reason = spawn.codex_resume_readiness(
            name, capture=lambda session: capture_pane(config, machine, session))
        conversation_id = launch["conversation_id"]
        result["conversationId"] = conversation_id
        result["resumed"] = bool(conversation_id) and readiness == "ready"
        result["conversationStatus"] = (
            ("resumed" if conversation_id else "fresh") if readiness == "ready" else readiness)
        if reason:
            warnings.append(f"Codex not confirmed resumed: {reason}; inspect {name} on machine "
                            f"{machine['name']} before sending a ruling")
    if reconcile:
        result["reconcile"] = True
    if warnings:
        result["warnings"] = warnings
    return result


# ---------------------------------------------------------------------------
# End session and cleanup
# ---------------------------------------------------------------------------

def _end_args(machine, name, wt, not_after, strict):
    return _sh(_END_SCRIPT, name, "" if not_after is None else str(int(not_after)), wt,
               "strict" if strict else "lenient", SWEEP_PROGRAM, *tmux_argv(machine))


def _pids(text):
    return [int(p) for p in (text or "").split() if p.isdigit()]


def end_session(config, project, task_id):
    """POST /api/end-session for a task on a machine (`project` placed
    there, machines.placed): kill the task
    session and every window of it linked elsewhere (by window id), then
    sweep the worktree's leftover processes on the worker. Returns
    (http status, body). A worker that cannot be reached -- or whose
    answer never came back -- gets the teardown recorded as owed (202),
    retried when it answers again; nothing is ended on the hub's guess."""
    machine = machine_for(config, project)
    name = spawn.session_name(project["name"], task_id)
    wt = worktree_path(machine, project["name"], task_id)
    result = machines.run_on_machine(config, machine["name"], _end_args(machine, name, wt, None, True))
    if result.status != ANSWERED:
        entry = record_owed({"kind": "end-session", "project": project["name"], "taskId": task_id,
                             "machine": machine["name"], "requestedAt": time.time(),
                             "reason": _reason(result)})
        return 202, {
            "ok": False, "owed": True, "session": name, "machine": machine["name"],
            "owedId": entry["id"],
            "message": (f"machine {machine['name']} did not answer ({_reason(result)}); ending "
                        f"{name} is recorded as owed and will be retried when it answers"),
        }
    if result.returncode == 3:
        return 404, {"error": f"no live session for {project['name']}/{task_id} on machine {machine['name']}"}
    if result.returncode == 4:
        return 500, {"error": f"machine {machine['name']}: tmux kill-session failed: {_reason(result)}"}
    server.record_session_ended(project["name"], task_id)
    body = {"ok": True, "session": name, "machine": machine["name"],
            "killedWindows": (_marker(result.stdout, "centrale-ended") or "").split(),
            "killedOrphans": _pids(_marker(result.stdout, "centrale-swept"))}
    if result.returncode == 6:
        body["warnings"] = [f"machine {machine['name']}: the session ended, but the orphan sweep "
                            f"could not run there (python3 missing?): {_reason(result)}"]
    return 200, body


def _cleanup_args(machine, project, task_id, expected, force):
    return _sh(_CLEANUP_SCRIPT, machines.repo_git_dir_path(machine, project),
               worktree_path(machine, project["name"], task_id), spawn.branch_name(task_id),
               expected or "", "1" if force else "0", SWEEP_PROGRAM)


def _merged_on_hub(project, task_id, ref):
    """The cleanup decision on the hub, against the fetched copy: the task
    Done on main (task-45's rule) and the fetched tip an ancestor of the
    base branch. Returns the verified sha, or None."""
    repo = project["path"]
    sha = server.run_git(["rev-parse", "--verify", "--quiet", ref], cwd=repo)
    if sha.returncode != 0:
        return None
    status = server._read_main_task_status(repo, task_id)
    if str(status or "").strip().lower() != "done":
        return None
    base = spawn.current_branch(repo)
    ancestor = server.run_git(["merge-base", "--is-ancestor", ref, base], cwd=repo)
    return sha.stdout.strip() if ancestor.returncode == 0 else None


def _forget_on_hub(project, machine, task_id):
    server.run_git(["update-ref", "-d", tracking_ref(machine["name"], task_id)], cwd=project["path"])
    return spawn.unlock_task_file(project["path"], task_id)


def _cleanup_body(project, machine, task_id, result):
    stdout = result.stdout
    discarded = []
    if "centrale-status-begin" in stdout:
        status = stdout.split("centrale-status-begin\n", 1)[1].split("centrale-status-end", 1)[0]
        discarded = server._dirty_paths_from_status(status)
    return {
        "ok": True,
        "branch": spawn.branch_name(task_id),
        "machine": machine["name"],
        "worktreeRemoved": _marker(stdout, "centrale-removed") is not None,
        "discardedPaths": discarded,
        "killedOrphans": _pids(_marker(stdout, "centrale-killed")),
        "taskFileUnlocked": _forget_on_hub(project, machine, task_id),
    }


def cleanup(config, project, task_id, force=False):
    """POST /api/cleanup-branch for a task on a machine, under the task's
    lifecycle lock (the caller holds it). The worker's session, checkout
    and branch tip are read in one round trip; the branch is fetched and
    the merged decision made on the HUB against that fresh copy; the
    worker then removes the worktree and branch only if its tip is still
    the one verified (bound to what was checked). Returns (status, body)."""
    machine = machine_for(config, project)
    name = spawn.session_name(project["name"], task_id)
    branch = spawn.branch_name(task_id)
    ref = tracking_ref(machine["name"], task_id)
    bare = machines.repo_git_dir_path(machine, project)
    tip_result = machines.run_on_machine(
        config, machine["name"], ["git", "--git-dir", bare, "rev-parse", "-q", "--verify", f"refs/heads/{branch}"])
    try:
        if tip_result.status != ANSWERED:
            raise _step_error(machine["name"], "read the task branch", tip_result)
        sessions, home, checkouts = survey(config, machine, project, task_id)
    except SpawnError as exc:
        if exc.status not in (503, 504):
            return exc.status, {"error": exc.message}
        # Unreachable: owed only for a branch the hub's last fetch already
        # shows merged -- and only that exact tip will be removed later.
        verified = _merged_on_hub(project, task_id, ref)
        if verified is None:
            return 503, {"error": f"{exc.message}; the hub's last fetched copy of {branch} is not "
                                  f"verified merged, so nothing is recorded"}
        entry = record_owed({"kind": "cleanup", "project": project["name"], "taskId": task_id,
                             "machine": machine["name"], "requestedAt": time.time(),
                             "expectedTip": verified, "force": bool(force), "reason": exc.message})
        return 202, {"ok": False, "owed": True, "branch": branch, "machine": machine["name"],
                     "owedId": entry["id"],
                     "message": f"{exc.message}; cleanup of {branch} at {verified[:12]} is recorded as "
                                f"owed and will be retried when the machine answers"}
    if name in {s["name"] for s in sessions}:
        return 409, {"error": f"a live tmux session is still running on machine {machine['name']}: {name}"}
    tip = tip_result.stdout.strip() if tip_result.returncode == 0 else None
    expected_path = expand(worktree_path(machine, project["name"], task_id), home)
    checked_out = checkouts.get(branch)
    if checked_out is not None and checked_out.rstrip("/") != expected_path.rstrip("/"):
        return 409, {"error": spawn.external_checkout_reason(branch, f"{machine['name']}:{checked_out}")}
    if tip is None and checked_out is None:
        return 404, {"error": f"no worktree or branch found for {project['name']}/{task_id} on machine "
                              f"{machine['name']}"}
    if tip is not None:
        url = git_url(machine, bare)
        fetched = run_hub_git(["fetch", "--quiet", "--no-write-fetch-head", url, f"+refs/heads/{branch}:{ref}"],
                              project["path"])
        if fetched.returncode != 0:
            return 502, {"error": f"machine {machine['name']}: fetching {branch} failed: "
                                  f"{(fetched.stderr or fetched.stdout).strip()}"}
        verified = _merged_on_hub(project, task_id, ref)
        if verified != tip:
            return 409, {"error": f"{branch} on machine {machine['name']} is not fully merged into the base branch"}
    result = machines.run_on_machine(config, machine["name"], _cleanup_args(machine, project, task_id, tip, force))
    if result.status != ANSWERED:
        if tip is None:
            return 504, {"error": f"machine {machine['name']}: outcome of the cleanup unknown ({_reason(result)})"}
        entry = record_owed({"kind": "cleanup", "project": project["name"], "taskId": task_id,
                             "machine": machine["name"], "requestedAt": time.time(),
                             "expectedTip": tip, "force": bool(force), "reason": _reason(result)})
        return 202, {"ok": False, "owed": True, "branch": branch, "machine": machine["name"],
                     "owedId": entry["id"],
                     "message": f"machine {machine['name']} did not confirm the cleanup "
                                f"({_reason(result)}); it is recorded as owed and will be retried"}
    if result.returncode == 7:
        return 409, {"error": f"{branch} moved on machine {machine['name']} since it was verified merged "
                              f"(now {_marker(result.stdout, 'centrale-moved')}); nothing was removed"}
    if result.returncode == 8:
        pids = _marker(result.stdout, "centrale-busy")
        return 409, {"error": f"processes still running with cwd under the worktree on machine "
                              f"{machine['name']} (pids: {', '.join(pids.split())}) -- pass force to kill "
                              "them and remove anyway"}
    if result.returncode == 12:
        status_output = result.stdout.split("centrale-status-begin\n", 1)[1].split("centrale-status-end", 1)[0]
        return 409, {"error": "worktree has uncommitted changes; pass force to discard them",
                     "discardedPaths": server._dirty_paths_from_status(status_output)}
    if result.returncode != 0:
        return 500, {"error": f"machine {machine['name']}: cleanup failed (exit {result.returncode}): "
                              f"{_reason(result)}"}
    return 200, _cleanup_body(project, machine, task_id, result)


# ---------------------------------------------------------------------------
# Owed teardowns (doc-3: "recorded as owed and retried when it is back")
# ---------------------------------------------------------------------------
#
# A request, not task state: "the owner asked for this teardown and the
# machine never heard it". Each entry is bound to what was asked -- an
# owed End session ends only a session created before the request (clock
# skew within --check's tolerance), an owed cleanup removes only the
# branch tip the hub verified -- and is dropped, with a stated outcome,
# when that is no longer what is there. Persisted so a hub restart does
# not silently forget a request; the file exists only while something is
# owed.

_owed_lock = threading.Lock()
_settle_locks = {}
_settle_guard = threading.Lock()


def owed_path():
    override = os.environ.get(OWED_ENV)
    if override:
        return override
    state = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    return os.path.join(state, "centrale", "owed-teardowns.json")


def _read_owed():
    try:
        with open(owed_path(), encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return []
    except (OSError, ValueError):
        return []
    return [e for e in data if isinstance(e, dict)] if isinstance(data, list) else []


def _write_owed(entries):
    path = owed_path()
    if not entries:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".owed-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(entries, f, indent=1)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def record_owed(entry):
    """Add (or replace: one per kind per task) an owed teardown."""
    entry = {"id": secrets.token_hex(6), **entry}
    with _owed_lock:
        entries = [e for e in _read_owed()
                   if not (e.get("kind") == entry["kind"] and e.get("project") == entry["project"]
                           and str(e.get("taskId")).lower() == str(entry["taskId"]).lower())]
        entries.append(entry)
        _write_owed(entries)
    return entry


def owed_teardowns():
    with _owed_lock:
        return _read_owed()


def _drop_owed(entry_id):
    with _owed_lock:
        _write_owed([e for e in _read_owed() if e.get("id") != entry_id])


def _settle_entry(config, entry):
    """Run one owed teardown. Returns (outcome, keep)."""
    project = spawn._find_project(config, entry.get("project"))
    if project is None or entry.get("machine") not in (project.get("machines") or []):
        return "dropped: the project no longer lists that machine", False
    project = machines.placed(project, entry["machine"])
    machine = machine_for(config, project)
    task_id = entry["taskId"]
    if entry["kind"] == "end-session":
        not_after = float(entry["requestedAt"]) + machines.CLOCK_SKEW_TOLERANCE_SECONDS
        name = spawn.session_name(project["name"], task_id)
        result = machines.run_on_machine(config, machine["name"], _end_args(
            machine, name, worktree_path(machine, project["name"], task_id), not_after, False))
        if result.status != ANSWERED:
            return f"still owed: {_reason(result)}", True
        if result.returncode == 5:
            return "dropped: a session newer than the request is running; it was left alone", False
        if result.returncode in (0, 6):
            server.record_session_ended(project["name"], task_id)
            return "done", False
        return f"still owed: exit {result.returncode}: {_reason(result)}", True
    if entry["kind"] == "cleanup":
        sessions_result = machines.run_on_machine(config, machine["name"],
                                                  _sh(_SESSIONS_SCRIPT, *tmux_argv(machine)))
        if not sessions_result.ok:
            return f"still owed: {_reason(sessions_result)}", True
        name = spawn.session_name(project["name"], task_id)
        if name in {s["name"] for s in _parse_sessions(sessions_result.stdout)}:
            return "dropped: a session is live on the task again", False
        result = machines.run_on_machine(config, machine["name"], _cleanup_args(
            machine, project, task_id, entry.get("expectedTip"), entry.get("force")))
        if result.status != ANSWERED:
            return f"still owed: {_reason(result)}", True
        if result.returncode == 7:
            return "dropped: the branch moved since it was verified merged; nothing was removed", False
        if result.returncode == 8:
            return "dropped: processes are running in the worktree; clean up again to decide", False
        if result.returncode == 12:
            status_output = result.stdout.split("centrale-status-begin\n", 1)[1].split("centrale-status-end", 1)[0]
            paths = server._dirty_paths_from_status(status_output)
            return f"dropped: worktree has uncommitted changes ({', '.join(paths)}); clean up again to decide", False
        if result.returncode == 0:
            _forget_on_hub(project, machine, task_id)
            return "done", False
        return f"still owed: exit {result.returncode}: {_reason(result)}", True
    return "dropped: unknown kind", False


def settle_owed(config, machine_name, project_name=None, task_id=None, holding_task_lock=False):
    """Retry the teardowns owed to one machine (optionally one task's).
    Each runs under its task's lifecycle lock -- taken without waiting, so
    a background retry never queues behind a merge; `holding_task_lock`
    when the caller already holds that one task's lock (a spawn or resume
    route). Returns [(entry, outcome)]; with nothing owed it does nothing,
    not even a round trip."""
    entries = [e for e in owed_teardowns() if e.get("machine") == machine_name
               and (project_name is None or e.get("project") == project_name)
               and (task_id is None or str(e.get("taskId")).lower() == task_id.lower())]
    if not entries:
        return []
    with _settle_guard:
        guard = _settle_locks.setdefault(machine_name, threading.Lock())
    if not guard.acquire(blocking=project_name is not None):
        return []
    outcomes = []
    try:
        for entry in entries:
            lock = None if holding_task_lock else server.task_lifecycle_lock(entry["project"], entry["taskId"])
            if lock is not None and not lock.acquire(blocking=False):
                outcomes.append((entry, "still owed: the task is busy"))
                continue
            try:
                outcome, keep = _settle_entry(config, entry)
            finally:
                if lock is not None:
                    lock.release()
            if not keep:
                _drop_owed(entry["id"])
            outcomes.append((entry, outcome))
    finally:
        guard.release()
    return outcomes


# ---------------------------------------------------------------------------
# Fetching task branches back (doc-3, "Running")
# ---------------------------------------------------------------------------
#
# The fetched refs are git's own copies; the time of the last successful
# fetch is this process's own observation, shown as an age so a stale copy
# is visible -- and unknown (None) after a restart until the next fetch.

_fetches = {}
_fetch_locks = {}
_fetch_guard = threading.Lock()


def fetch_task_branches(config, machine, project, timeout=None):
    """`git fetch --prune` of every task/* branch in the worker's bare
    repository into refs/remotes/<machine>/task/* on the hub. One fetch per
    project at a time: a concurrent refresh skips rather than racing git's
    ref locks."""
    key = (project["name"], machine["name"])
    with _fetch_guard:
        lock = _fetch_locks.setdefault(key, threading.Lock())
    if not lock.acquire(blocking=False):
        return
    try:
        url = git_url(machine, machines.repo_git_dir_path(machine, project))
        proc = run_hub_git(["fetch", "--quiet", "--prune", "--no-write-fetch-head", url,
                            f"+refs/heads/task/*:refs/remotes/{machine['name']}/task/*"],
                           project["path"], timeout=timeout)
        state = dict(_fetches.get(key) or {"at": None})
        if proc.returncode == 0:
            state.update(at=time.time(), error=None)
        else:
            state["error"] = (proc.stderr or proc.stdout or f"git fetch exited {proc.returncode}").strip()
        _fetches[key] = state
    finally:
        lock.release()


def fetch_status(project, machine_name, now=None):
    """What a board column carries for each of a project's machines: the
    age of its last successful task/* fetch from there (None: none since
    this process started) and the last attempt's error, if it failed."""
    state = _fetches.get((project["name"], machine_name)) or {}
    at = state.get("at")
    now = time.time() if now is None else now
    return {
        "machine": machine_name,
        "fetchedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(at)) if at else None,
        "fetchAgeSeconds": max(0, int(now - at)) if at else None,
        "error": state.get("error"),
    }


def after_refresh(config, report):
    """Run after a board refresh's reachability report: fetch every
    project on each machine that answered (in parallel, inside the
    refresh budget), and retry what is owed to those machines in the
    background, so no request waits on a teardown."""
    reachable = [r["name"] for r in report if r["reach"] == machines.REACH[ANSWERED]]
    jobs = [(machines.machine_named(config, name), project)
            for name in reachable for project in machines.projects_on(config, name)]
    if jobs:
        budget = machines.refresh_budget(config)
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, len(jobs))) as pool:
            for future in [pool.submit(fetch_task_branches, config, m, p, budget) for m, p in jobs]:
                future.result()
    for name in reachable:
        if any(e.get("machine") == name for e in owed_teardowns()):
            threading.Thread(target=settle_owed, args=(config, name), daemon=True).start()
