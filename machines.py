"""Remote machines (doc-3, task-206): the one way Centrale runs a command
on another Linux machine, and what `--check` asks each of them.

Every remote operation -- git, backlog, file checks, tmux, process sweeps --
goes through run_batch() (run_on_machine() is its one-command form), so
tests fake it the way they fake server.run_git and server.run_tmux. The
hub (machine name None) runs locally through server._run; any
configured machine runs over ssh. With no `machines` in projects.json
nothing here is ever called: no ssh process, no control socket, no file.

Results are three-way, because "the command failed" and "the machine never
heard the command" call for different actions:

- ``unreached``: ssh never started the command (exit 255 or the time
  budget ran out before the worker's shell printed its start marker);
- ``answered``: the command ran to completion and its exit code is real;
- ``unknown``: the worker started the batch but this command's result
  never came back (connection dropped, budget ran out mid-batch). Callers
  take the path that destroys nothing.

The command line on the worker is always ``sh -c <script>`` built with
POSIX quoting (shlex), since prompts are multi-line and Backlog.md task
filenames contain spaces. Paths that belong to the worker are MachinePath
values: the hub never expands their ``~`` (its home is not the worker's),
the worker's shell does.

Python 3.12 stdlib only. Calls into `server` go through the module object
at call time, like spawn.py and harvest.py.
"""

from __future__ import annotations

import concurrent.futures
import dataclasses
import math
import os
import re
import secrets
import shlex
import subprocess
import tempfile
import threading
import time

import server
import version

# ssh options from thurbox, as measured in the task-205 spike. Every
# invocation carries ConnectTimeout, mux clients included: a client riding
# a frozen master without it hung forever, with it gave up in ~3 s and
# fell back to a direct connection.
SSH_OPTIONS = (
    ("BatchMode", "yes"),
    ("ConnectTimeout", "5"),
    ("ServerAliveInterval", "5"),
    ("ServerAliveCountMax", "3"),
    ("ControlMaster", "auto"),
    ("ControlPersist", "60"),
)
SSH_DIR_ENV = "CENTRALE_SSH_DIR"
# ssh refuses a ControlPath over ~108 bytes, and while a master starts it
# appends a 17-character random suffix to the 40-character %C hash -- so
# the directory itself has to stay short.
MAX_SSH_DIR_LENGTH = 48
SSH_UNREACHED_EXIT = 255

# Worker prerequisites (doc-3, "--check per machine").
MIN_TMUX_VERSION = (3, 2)  # pane options need 3.0; refresh-client -B 3.2
CLOCK_SKEW_TOLERANCE_SECONDS = 2.0
# Exit code 0 means logged in, for the two CLIs that can say so cheaply.
LOGIN_PROBES = {"claude": ("auth", "status"), "codex": ("login", "status")}

UNREACHED, ANSWERED, UNKNOWN = "unreached", "answered", "unknown"


class MachinePath(str):
    """A path on a worker, `~` and all. Rendered into the worker's script
    so ITS shell expands a leading `~/` (see shell_word); on the hub it is
    expanded locally like any other path."""


@dataclasses.dataclass
class MachineResult:
    status: str  # UNREACHED | ANSWERED | UNKNOWN
    returncode: int | None
    stdout: str
    stderr: str
    machine: str | None = None

    @property
    def ok(self):
        return self.status == ANSWERED and self.returncode == 0


# ---------------------------------------------------------------------------
# Config lookups
# ---------------------------------------------------------------------------

def configured(config):
    return bool(config.get("machines"))


def machine_named(config, name):
    """The machine dict for `name`, or None for the hub (None)."""
    if name is None:
        return None
    for machine in config.get("machines") or []:
        if machine["name"] == name:
            return machine
    raise KeyError(f"unknown machine: {name!r}")


def projects_on(config, name):
    """The projects whose tasks may run on machine `name` (task-224: a
    project lists its machines; the hub is always allowed)."""
    return [p for p in config.get("projects") or [] if name in (p.get("machines") or [])]


def placed(project, machine_name):
    """`project` as seen by one task placed on `machine_name` (None for the
    hub): a copy carrying "machine", the key remote.py and
    remote_harvest.py dispatch on. Derived per task on every request from
    the task's location (locate), never stored in the config."""
    return {**project, "machine": machine_name}


def tmux_argv(machine):
    """How to call tmux on `machine`: its own server named by the optional
    `tmuxSocket` (`tmux -L`), else that user's default one (task-209)."""
    socket = (machine or {}).get("tmuxSocket")
    return ["tmux", "-L", socket] if socket else ["tmux"]


# task-235: the parked ordinary checkout is also the source of worktreeLinks.
# Its HEAD stays detached so the hub may push main without touching it.
def repo_git_dir_path(machine, project):
    return MachinePath(f"{machine['repoRoot'].rstrip('/')}/{project['name']}/.git")


def links_source_path(machine, project, relative):
    return MachinePath(f"{machine['repoRoot'].rstrip('/')}/{project['name']}/{relative}")


def _copy_from_hub(machine, project, relative):
    """Stream one hub path to the parked worker repository without a temp copy."""
    source = shlex.quote(os.path.expanduser(project["path"]))
    repo = shell_word(MachinePath(f"{machine['repoRoot'].rstrip('/')}/{project['name']}"))
    remote = f"mkdir -p {repo} && tar -C {repo} -xf -"
    return (f"tar -C {source} -cf - {shlex.quote(relative)} | "
            f"ssh {shlex.quote(machine['ssh'])} {shlex.quote(remote)}")


def _missing_link_command(machine, project, relative):
    # task-233: package managers run beside the link, which may be nested.
    parent = os.path.dirname(relative)
    leaf = os.path.basename(relative)
    worker_parent = shell_word(MachinePath(
        f"{machine['repoRoot'].rstrip('/')}/{project['name']}" + (f"/{parent}" if parent else "")))
    hub_root = os.path.expanduser(project["path"])
    hub_parent = shlex.quote(os.path.join(hub_root, parent) if parent else hub_root)
    target = shell_word(links_source_path(machine, project, relative))
    if leaf == ".venv":
        # task-233: a requirements file lives only for this command's duration.
        remote = (f"set -e; mkdir -p {worker_parent}; python3 -m venv {target}; req=$(mktemp); "
                  f"trap 'rm -f \"$req\"' EXIT; cat > \"$req\"; "
                  f"{target}/bin/python -m pip install -r \"$req\"")
        return (f"{hub_parent}/.venv/bin/python -m pip freeze --exclude-editable | "
                f"ssh {shlex.quote(machine['ssh'])} {shlex.quote(remote)}")
    if leaf == "node_modules":
        remote = (f"mkdir -p {worker_parent} && tar -C {worker_parent} -xf - && "
                  f"cd {worker_parent} && npm ci")
        return (f"tar -C {hub_parent} -cf - package.json package-lock.json | "
                f"ssh {shlex.quote(machine['ssh'])} "
                f"{shlex.quote(remote)}")
    return _copy_from_hub(machine, project, relative)


def _ignored_check_files(project):
    """Top-level ignored dotfiles beside the hub checkout for a checkCommand.

    Ignore directories (venvs and caches): they are covered by worktreeLinks
    or project-specific setup. The git boundary is injectable in tests.
    """
    if not project.get("checkCommand"):
        return []
    result = server.run_git(["ls-files", "--others", "--ignored", "--exclude-standard",
                             "--directory", "-z"], cwd=project["path"])
    if result.returncode != 0:
        return []
    links = set(project.get("worktreeLinks") or [])
    return sorted(path for path in (result.stdout or "").split("\0")
                  if path.startswith(".") and "/" not in path and path not in links)


# ---------------------------------------------------------------------------
# ssh
# ---------------------------------------------------------------------------

def ssh_dir():
    """Where the generated ssh config and the control sockets live:
    $CENTRALE_SSH_DIR, else $XDG_RUNTIME_DIR/centrale-ssh, else
    <tmp>/centrale-ssh-<uid>. Short on purpose (MAX_SSH_DIR_LENGTH)."""
    override = os.environ.get(SSH_DIR_ENV)
    if override:
        return override
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime:
        return os.path.join(runtime, "centrale-ssh")
    return os.path.join(tempfile.gettempdir(), f"centrale-ssh-{os.getuid()}")


def ssh_config_text(directory):
    """An ssh config whose defaults sit BELOW the user's own: ssh takes the
    first value it reads for each option, so including ~/.ssh/config first
    lets every value the user set there win, as `-o` on the command line
    could not. The system config comes last, as ssh would read it."""
    lines = [
        "# Generated by Centrale for its connections to machines (task-206).",
        "# Rewritten on use; edit ~/.ssh/config instead -- its values win.",
        f"Include {os.path.expanduser('~/.ssh/config')}",
        "Host *",
        *(f"  {key} {value}" for key, value in SSH_OPTIONS),
        f"  ControlPath {directory}/%C",
        "Include /etc/ssh/ssh_config",
    ]
    return "\n".join(lines) + "\n"


def ssh_config_path():
    """The generated config, written (owner-only) only when its content
    changed. Raises OSError for a directory ssh could not use."""
    directory = ssh_dir()
    if len(directory) > MAX_SSH_DIR_LENGTH:
        raise OSError(
            f"ssh control directory {directory} is longer than {MAX_SSH_DIR_LENGTH} "
            f"characters, too long for ssh's socket paths -- set {SSH_DIR_ENV} to a shorter one"
        )
    os.makedirs(directory, mode=0o700, exist_ok=True)
    path = os.path.join(directory, "config")
    text = ssh_config_text(directory)
    try:
        with open(path, encoding="utf-8") as f:
            if f.read() == text:
                return path
    except FileNotFoundError:
        pass
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".config.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    return path


def ssh_command():
    return ["ssh", "-F", ssh_config_path()]


def git_ssh_env():
    """Environment for git's own ssh calls (push, fetch): without it each
    opens a fresh connection (~0.3 s on the spike's LAN) instead of riding
    the kept-open master."""
    return {"GIT_SSH_COMMAND": shlex.join(ssh_command())}


def _drain(stream, sink):
    try:
        while chunk := stream.read1(65536):
            sink.append(chunk)
    except (OSError, ValueError):
        pass


def run_ssh(argv, input=None, timeout=None):
    """The subprocess boundary under run_batch: returns (returncode,
    stdout, stderr, timed_out). Unlike server._run, a timeout keeps the
    output produced so far -- it is how a batch that started tells its
    finished commands from its unfinished ones -- and does not wait for
    the pipes to close: a client riding a kept-open master has handed
    them to the master, which holds them until the remote command ends."""
    try:
        proc = subprocess.Popen(
            argv, stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
    except FileNotFoundError as exc:  # no ssh on the hub: it could not even try
        return None, "", str(exc), False
    out, err = [], []
    readers = [threading.Thread(target=_drain, args=(proc.stdout, out), daemon=True),
               threading.Thread(target=_drain, args=(proc.stderr, err), daemon=True)]
    for reader in readers:
        reader.start()
    if input is not None:
        try:
            proc.stdin.write(input.encode())
            proc.stdin.close()
        except OSError:
            pass
    timed_out = False
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        proc.kill()
        proc.wait()
    for reader in readers:
        reader.join(timeout=None if not timed_out else 0.5)

    def text(chunks):
        return b"".join(chunks).decode("utf-8", errors="replace")

    return proc.returncode, text(out), text(err), timed_out


# ---------------------------------------------------------------------------
# The script a worker runs
# ---------------------------------------------------------------------------

def shell_word(arg):
    """One argv element as a POSIX shell word. A MachinePath's leading `~`
    becomes "$HOME", so the worker's shell expands it and the hub never
    does; everything else is quoted literally."""
    if isinstance(arg, MachinePath):
        if arg == "~":
            return '"$HOME"'
        if arg.startswith("~/"):
            return '"$HOME"/' + shlex.quote(arg[2:])
    return shlex.quote(str(arg))


def batch_script(nonce, commands, stdin=False):
    """A POSIX sh script running each (argv, cwd) in turn, framing each
    one's stdout, exit code and stderr between nonce markers. The first
    line it prints proves the worker's shell ran: anything before it is
    ssh failing, never a command."""
    lines = [
        'e=$(mktemp) || exit 125',
        "trap 'rm -f \"$e\"' EXIT",
        f"echo {nonce} start",
    ]
    for index, (argv, cwd) in enumerate(commands):
        cd = f"cd {shell_word(cwd)} && " if cwd else ""
        redirect = "" if stdin else " </dev/null"
        lines.append(f"( {cd}exec {' '.join(shell_word(a) for a in argv)} ){redirect} 2>\"$e\"; r=$?")
        lines.append(f"printf '\\n{nonce} {index} rc %s\\n' \"$r\"; cat \"$e\"; printf '\\n{nonce} {index} end\\n'")
    return "\n".join(lines) + "\n"


def parse_batch_output(nonce, count, stdout):
    """(started, [(returncode, stdout, stderr) or None per command])."""
    marker = f"{nonce} start\n"
    at = stdout.find(marker)
    if at < 0:
        return False, [None] * count
    rest = stdout[at + len(marker):]
    parsed = []
    for index in range(count):
        rc = re.search(rf"\n{nonce} {index} rc (\d+)\n", rest)
        end = rest.find(f"\n{nonce} {index} end\n", rc.end() - 1) if rc else -1
        if not rc or end < 0:
            parsed.extend([None] * (count - index))
            break
        parsed.append((int(rc.group(1)), rest[:rc.start()], rest[rc.end():end]))
        rest = rest[end + len(f"\n{nonce} {index} end\n"):]
    return True, parsed


# ---------------------------------------------------------------------------
# The run-on-machine function
# ---------------------------------------------------------------------------

def _local(arg):
    return os.path.expanduser(arg) if isinstance(arg, MachinePath) else arg


def run_batch(config, machine_name, commands, timeout=None, input=None):
    """Run `commands` -- a list of (argv, cwd-or-None) -- on one machine in
    ONE round trip, returning a MachineResult per command in order.
    `machine_name` None is the hub: run locally, as server._run would.
    `timeout` is the whole batch's budget (default: the shared subprocess
    timeout). `input` is the first command's stdin; the rest read
    /dev/null."""
    if timeout is None:
        timeout = server._subprocess_timeout_seconds
    machine = machine_named(config, machine_name)
    commands = [(list(argv), cwd) for argv, cwd in commands]
    if machine is None:
        results = []
        for index, (argv, cwd) in enumerate(commands):
            proc = server._run([_local(a) for a in argv], cwd=_local(cwd) if cwd else None,
                               timeout=timeout, input=input if index == 0 else None)
            results.append(MachineResult(ANSWERED, proc.returncode, proc.stdout or "",
                                         proc.stderr or "", None))
        return results

    nonce = "centrale-" + secrets.token_hex(8)
    if input is not None and len(commands) > 1:
        raise ValueError("input is only supported for a one-command batch")
    try:
        ssh = ssh_command()
    except OSError as exc:
        return [MachineResult(UNREACHED, None, "", str(exc), machine_name) for _ in commands]
    script = batch_script(nonce, commands, stdin=input is not None)
    # The worker enforces the budget too (`timeout -k`): killing the
    # local ssh client alone leaves the remote command running (task-205).
    limit = max(1, math.ceil(timeout))
    remote = f"timeout -k 2 {limit} sh -c {shlex.quote(script)}"
    argv = [*ssh, machine["ssh"], remote]
    returncode, stdout, stderr, timed_out = run_ssh(argv, input=input, timeout=timeout)
    started, parsed = parse_batch_output(nonce, len(commands), stdout)
    if not started:
        status = UNREACHED if timed_out or returncode in (SSH_UNREACHED_EXIT, None) else UNKNOWN
        reason = (stderr or "").strip() or (
            f"no answer within {timeout:g}s" if timed_out else f"ssh exited {returncode}")
        return [MachineResult(status, None, "", reason, machine_name) for _ in commands]
    results = []
    for entry in parsed:
        if entry is None:
            reason = f"no result within {timeout:g}s" if timed_out else (
                (stderr or "").strip() or "the connection ended before this command finished")
            results.append(MachineResult(UNKNOWN, None, "", reason, machine_name))
        else:
            rc, out, err = entry
            results.append(MachineResult(ANSWERED, rc, out, err, machine_name))
    return results


def run_on_machine(config, machine_name, argv, cwd=None, timeout=None, input=None):
    """One command on one machine: run_batch with a single entry."""
    return run_batch(config, machine_name, [(argv, cwd)], timeout=timeout, input=input)[0]


def run_on_machines(config, batches, timeout=None):
    """{machine name: [(argv, cwd)]} -> {machine name: [MachineResult]},
    one run_batch per machine, all machines in parallel, so an
    unreachable one costs its own budget and nobody else's."""
    if not batches:
        return {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(batches)) as pool:
        futures = {name: pool.submit(run_batch, config, name, commands, timeout)
                   for name, commands in batches.items()}
        return {name: future.result() for name, future in futures.items()}


def refresh_budget(config):
    """Per-machine time budget for one board or fleet refresh: short of
    the refresh interval, so one unreachable machine never stretches a
    refresh past it, and never longer than the shared subprocess timeout."""
    interval = config.get("refreshIntervalSeconds") or server.DEFAULT_REFRESH_INTERVAL_SECONDS
    return max(1.0, min(float(server._subprocess_timeout_seconds), interval - 2.0))


REACH = {ANSWERED: "reachable", UNREACHED: "unreachable", UNKNOWN: "unknown"}


def refresh(config, reads=None):
    """One board or fleet refresh's remote reads: ONE round trip per
    configured machine (doc-3: five reads kept-open cost 52 ms as five
    round trips and 18 ms as one). `reads` maps a machine name to the
    (argv, cwd) list a refresh wants from it; a machine with none is
    still asked `true`, which is its reachability probe.

    Returns [{"name", "reach", "error", "results"}] in config order, or
    [] with no machines configured -- in which case nothing runs."""
    machines = config.get("machines") or []
    if not machines:
        return []
    reads = reads or {}
    batches = {m["name"]: list(reads.get(m["name"]) or [(["true"], None)]) for m in machines}
    answers = run_on_machines(config, batches, timeout=refresh_budget(config))
    report = []
    for machine in machines:
        name = machine["name"]
        results = answers.get(name) or []
        statuses = {r.status for r in results}
        if statuses == {UNREACHED}:
            status = UNREACHED
        elif statuses == {ANSWERED}:
            status = ANSWERED
        else:
            status = UNKNOWN
        failed = next((r for r in results if r.status != ANSWERED), None)
        error = (failed.stderr.strip() or None) if failed else None
        report.append({"name": name, "reach": REACH[status], "error": error, "results": results})
    return report


def reach_summary(report):
    """What /api/board and /api/fleet carry: name, reach, error."""
    return [{"name": r["name"], "reach": r["reach"], "error": r["error"]} for r in report]


# ---------------------------------------------------------------------------
# Where a task is (doc-3 revision 5, task-224)
# ---------------------------------------------------------------------------
#
# A task's location is derived, never stored: it is where its task/<id>
# worktree is checked out -- the hub's `git worktree list`, or a machine's
# project-repository worktree list -- and, with no worktree anywhere, where
# its branch is (a local task/<id> means the hub; a branch only in a
# machine's project repository, seen through the hub's fetched
# refs/remotes/<machine>/task/<id>, means that machine). Evidence in two
# places is a conflict, reported with both and never guessed between. A
# machine that does not answer is represented by its fetched ref alone.

# Exits 0 with nothing when the project repository is not there yet: nothing
# was ever spawned on that machine for this project.
_WORKTREE_LIST = 'test -d "$1" || exit 0; exec git --git-dir="$1" worktree list --porcelain'

# Every task/* branch in the project repository; nothing when it is absent.
_BRANCH_LIST = ('test -d "$1" || exit 0; '
                'exec git --git-dir="$1" for-each-ref --format="%(refname)" refs/heads/task')

# Distinguish a missing project from the old bare layout. A branch checkout
# here would make the hub's push to that branch fail (task-235).
_REPO_PROBE = ('# centrale-repo-probe\n'
               'if [ -d "$1/.git" ] && '
               '[ "$(git -C "$1" rev-parse --is-inside-work-tree)" = true ] && '
               '[ "$(git -C "$1" rev-parse --abbrev-ref HEAD)" = HEAD ]; then '
               'echo parked; '
               'elif [ -d "$2" ] && '
               '[ "$(git --git-dir="$2" rev-parse --is-bare-repository)" = true ]; then '
               'echo legacy; '
               'else exit 1; fi')


def _task_branches(porcelain):
    prefix = "branch refs/heads/"
    return sorted(line[len(prefix):] for line in porcelain.splitlines()
                  if line.startswith(prefix + "task/"))


def _where(machine_name):
    return "the hub" if machine_name is None else f"machine {machine_name}"


def place_label(machine_name):
    """How the API names a place: "hub" or the machine's name."""
    return server.HUB if machine_name is None else machine_name


@dataclasses.dataclass
class Location:
    """Where one task is. `places` lists every place holding evidence of
    it (None for the hub), hub first, then in the project's machines
    order; `reach` maps a machine place to "reachable", "unreachable" or
    "unknown" (how its evidence was read)."""
    project: dict
    task_id: str
    places: list
    reach: dict = dataclasses.field(default_factory=dict)

    @property
    def found(self):
        return bool(self.places)

    @property
    def conflict(self):
        return len(self.places) > 1

    @property
    def machine(self):
        """The machine the task is on, None for the hub or no location (a
        conflict has no single machine either: callers refuse it first)."""
        return self.places[0] if len(self.places) == 1 else None

    def conflict_reason(self):
        listed = " and ".join(_where(p) for p in self.places)
        return (f"{self.task_id.upper()} is in two places at once ({listed}): Centrale does not "
                "guess which one is the task -- remove the copy that is not, then try again")

    def placed(self):
        """The project as seen by this task (see placed())."""
        return placed(self.project, self.machine)

    def as_json(self):
        """The API's "location": {"machine": "hub"|name|None (no location),
        "conflict": [places] or None, "reach": the machine's reach or None}."""
        if self.conflict:
            return {"machine": None, "conflict": [place_label(p) for p in self.places], "reach": None}
        if not self.places:
            return {"machine": None, "conflict": None, "reach": None}
        machine = self.places[0]
        return {"machine": place_label(machine), "conflict": None,
                "reach": self.reach.get(machine) if machine else None}


def decide(project, task_id, hub, machines_evidence):
    """The Location from evidence. `hub` is True when the hub has the
    task's worktree or local branch; `machines_evidence` maps each of the
    project's machines to (has_evidence, reach). Shared by the board
    (from a refresh's answers) and by locate (asked fresh)."""
    places = [None] if hub else []
    reach = {}
    for name in project.get("machines") or []:
        found, how = machines_evidence.get(name, (False, None))
        if found:
            places.append(name)
            reach[name] = how
    return Location(project, task_id, places, reach)


def fetched_task_branches(project, machine_name):
    """{task/<id>} the hub holds fetched copies of from `machine_name`
    (refs/remotes/<machine>/task/*): one for-each-ref, no ssh."""
    prefix = f"refs/remotes/{machine_name}/"
    proc = server.run_git(["for-each-ref", "--format=%(refname)", prefix + "task"], cwd=project["path"])
    if proc.returncode != 0:
        return set()
    return {line.strip()[len(prefix):] for line in (proc.stdout or "").splitlines()
            if line.strip().startswith(prefix)}


def _hub_has(project, task_id):
    """The hub's evidence for a task: its worktree or its local branch."""
    import spawn  # local import: spawn imports this module's callers

    repo = project["path"]
    if not os.path.isdir(repo):
        return False
    branch = spawn.branch_name(task_id)
    proc = server.run_git(["rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"], cwd=repo)
    if proc.returncode == 0:
        return True
    return branch in spawn.branch_checkouts(repo)


def _has_fetched(project, machine_name, task_id):
    import spawn  # local import: spawn imports this module's callers

    ref = f"refs/remotes/{machine_name}/{spawn.branch_name(task_id)}"
    return server.run_git(["rev-parse", "--verify", "--quiet", ref], cwd=project["path"]).returncode == 0


def locate(config, project, task_id, live=True):
    """Where `task_id` of `project` is, read fresh: the hub's worktree
    list and branch, and -- with `live` -- each of the project's machines
    asked in ONE round trip apiece, all in parallel (its bare
    repository's worktree list and task branches); a machine that does
    not answer, or every machine without `live`, is represented by the
    hub's fetched ref. A project with no machines is on the hub without
    a single command run (the single-machine guarantee)."""
    if not project.get("machines"):
        return Location(project, task_id, [None])
    import spawn  # local import: spawn imports this module's callers

    branch = spawn.branch_name(task_id)
    evidence = {}
    answers = {}
    if live:
        answers = run_on_machines(config, {
            name: [checkouts_command(config, project, name), branches_command(config, project, name)]
            for name in project["machines"]
        }, timeout=refresh_budget(config))
    for name in project["machines"]:
        results = answers.get(name)
        if results and all(r.ok for r in results):
            on_machine = (branch in _task_branches(results[0].stdout)
                          or f"refs/heads/{branch}" in results[1].stdout.split())
            evidence[name] = (on_machine, REACH[ANSWERED])
        else:
            how = REACH[UNKNOWN] if results and any(r.status != UNREACHED for r in results) \
                else REACH[UNREACHED]
            evidence[name] = (_has_fetched(project, name, task_id), how if live else None)
    return decide(project, task_id, _hub_has(project, task_id), evidence)


def checkouts_command(config, project, machine_name):
    """The (argv, cwd) listing `project`'s worktrees on one machine."""
    if machine_name is None:
        return ["git", "worktree", "list", "--porcelain"], project["path"]
    bare = repo_git_dir_path(machine_named(config, machine_name), project)
    return ["sh", "-c", _WORKTREE_LIST, "sh", bare], None


def branches_command(config, project, machine_name):
    """The (argv, cwd) listing `project`'s task/* branches in a machine's
    project repository."""
    bare = repo_git_dir_path(machine_named(config, machine_name), project)
    return ["sh", "-c", _BRANCH_LIST, "sh", bare], None


def checkouts_from(result):
    """(status, task branches checked out, reason) from checkouts_command's result."""
    if result.status != ANSWERED:
        return result.status, [], result.stderr.strip() or result.status
    if result.returncode != 0:
        return UNKNOWN, [], (result.stderr or result.stdout).strip() or f"git exited {result.returncode}"
    return ANSWERED, _task_branches(result.stdout), None


def _tasks_there(results):
    """(status, sorted task/* names with a worktree or a branch, reason)
    from [checkouts_command, branches_command] results on one machine."""
    status, checked_out, reason = checkouts_from(results[0])
    if status != ANSWERED:
        return status, [], reason
    listing = results[1]
    if listing.status != ANSWERED or listing.returncode != 0:
        return (listing.status if listing.status != ANSWERED else UNKNOWN, [],
                _result_reason(listing))
    prefix = "refs/heads/"
    branches = {line.strip()[len(prefix):] for line in listing.stdout.splitlines()
                if line.strip().startswith(prefix + "task/")}
    return ANSWERED, sorted(set(checked_out) | branches), None


def _result_reason(result):
    return (result.stderr or result.stdout or "").strip() or result.status


def machines_removal_refusal(config, project, removed):
    """Why `project` may not stop listing the machines in `removed` now,
    or None (task-224). A machine leaves a project's list only while the
    project has no task worktree and no task branch there -- those tasks
    would be left where nothing here can reach them -- and not knowing
    (the machine does not answer) refuses too, since removing it is the
    one step that would strand them. The hub's fetched refs from that
    machine count as well: they are the last word on an unreachable one."""
    if not removed:
        return None
    answers = run_on_machines(config, {
        name: [checkouts_command(config, project, name), branches_command(config, project, name)]
        for name in removed
    })
    problems = []
    for name in removed:
        status, tasks, reason = _tasks_there(answers[name])
        if status != ANSWERED:
            problems.append(f"cannot tell whether {_where(name)} still has a task of this project "
                            f"({reason}) -- try again once it answers")
            continue
        tasks = sorted(set(tasks) | fetched_task_branches(project, name))
        if tasks:
            problems.append(f"{_where(name)} still has this project's tasks ({', '.join(tasks)}) -- "
                            "merge, discard or abandon them first, or they are left where nothing "
                            "here can reach them")
    return "; ".join(problems) or None


def _fetched_machines(project):
    """{machine name: {task/<id>}} for every refs/remotes/<m>/task/* the
    hub holds for `project` from a machine. Centrale fetches by URL, never
    through a named git remote, so a namespace that IS a git remote (an
    `origin` the owner pushes task branches to) is the owner's, not a
    machine's, and is left out."""
    if not os.path.isdir(project.get("path") or ""):
        return {}
    remotes = server.run_git(["remote"], cwd=project["path"])
    if remotes.returncode != 0:
        return {}
    named = set((remotes.stdout or "").split())
    proc = server.run_git(["for-each-ref", "--format=%(refname)", "refs/remotes"], cwd=project["path"])
    found = {}
    if proc.returncode != 0:
        return found
    for line in (proc.stdout or "").splitlines():
        parts = line.strip().split("/", 3)
        if len(parts) == 4 and parts[3].startswith("task/") and parts[2] not in named:
            found.setdefault(parts[2], set()).add(parts[3])
    return found


def stranded_checkouts(config):
    """(refusals, warnings) for a projects.json edited by hand to take a
    machine away from a project -- or out of `machines` altogether --
    while that project still had tasks there (task-224): task worktrees
    or branches in the project repository of a configured machine the
    project no longer lists, or the hub's fetched refs from any machine
    the project no longer lists. A machine that does not answer is a
    warning (this cannot be decided), never a refusal. With no machines
    configured only the hub's own refs are read: no ssh."""
    machines = config.get("machines") or []
    configured = {m["name"] for m in machines}
    refusals, warnings = [], []
    # Local reads only (no ssh), so even a config whose last machine was
    # taken out is held to what the hub's fetched refs still say.
    for project in config.get("projects") or []:
        allowed = set(project.get("machines") or [])
        for name, tasks in sorted(_fetched_machines(project).items()):
            if name not in allowed:
                refusals.append(_stranded(project, name, sorted(tasks), fetched=True,
                                          configured=name in configured))
    if not machines:
        return refusals, warnings
    batches, asked = {}, {}
    for machine in machines:
        others = [p for p in config.get("projects") or [] if machine["name"] not in (p.get("machines") or [])]
        if others:
            batches[machine["name"]] = [command for p in others for command in
                                        (checkouts_command(config, p, machine["name"]),
                                         branches_command(config, p, machine["name"]))]
            asked[machine["name"]] = others
    for name, results in run_on_machines(config, batches).items():
        for index, project in enumerate(asked[name]):
            status, tasks, reason = _tasks_there(results[2 * index:2 * index + 2])
            if status == ANSWERED and tasks:
                refusals.append(_stranded(project, name, tasks))
            elif status != ANSWERED:
                warnings.append(f"{project['name']}: could not list its tasks on "
                                f"{_where(name)} ({reason})")
                break  # one line per machine that did not answer
    return refusals, warnings


def _stranded(project, machine_name, branches, fetched=False, configured=True):
    where = (f"the hub still holds fetched copies of its task branches from {_where(machine_name)}"
             if fetched else f"{_where(machine_name)} still has its tasks")
    back = (f"put {machine_name} back in the project's 'machines'" if configured else
            f"put machine {machine_name} back in 'machines' and in the project's 'machines'")
    return (f"{project['name']} does not list machine {machine_name} in its 'machines', but {where} "
            f"({', '.join(branches)}) -- {back}, and merge, discard or abandon those tasks before "
            "removing it")


# ---------------------------------------------------------------------------
# --check, per machine
# ---------------------------------------------------------------------------

def _parse_version(text):
    match = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", text or "")
    return tuple(int(g or 0) for g in match.groups()) if match else None


def _resolved_path(stdout):
    """The full path an interactive `command -v` printed. Startup files
    may print anything; the answer is the last line that is a path."""
    for line in reversed((stdout or "").splitlines()):
        line = line.strip()
        if line.startswith("/"):
            return line
    return None


def _resolve_probe(executable):
    """Resolves a CLI the way the owner's own terminal would: an
    INTERACTIVE shell. Ubuntu's ~/.bashrc returns early for anything
    else, so a per-user npm install (~/.npm-global/bin) is found neither
    by plain ssh nor by `bash -lc` (task-205)."""
    if executable.startswith("/"):
        return ["sh", "-c", 'test -x "$1" && echo "$1"', "sh", executable]
    return ["bash", "-ic", f"command -v {shlex.quote(executable)}"]


_CONTROL_MODE_PROBE = (
    # A private tmux server, never the worker's own sessions, removed after.
    's="centrale-check-$$"; '
    "printf 'display-message -p centrale-control-ok\\nkill-server\\n' | "
    'tmux -L "$s" -f /dev/null -C new-session -s probe "sleep 30" 2>&1; '
    'r=$?; rm -f "${TMUX_TMPDIR:-/tmp}/tmux-$(id -u)/$s"; exit $r'
)


def agent_executables(config):
    """Each distinct agent CLI (argv[0]) in the agents map, in order."""
    seen = []
    for entry in (config.get("agents") or {}).values():
        executable = entry["cmd"][0]
        if executable not in seen:
            seen.append(executable)
    return seen


def hub_tool_version(executable):
    path = server.which(os.path.expanduser(executable))
    if path is None:
        return None
    proc = server._run([path, "--version"], timeout=server._subprocess_timeout_seconds)
    return server._version_token((proc.stdout or "") + " " + (proc.stderr or "")) if proc.returncode == 0 else None


def doctor_check(config):
    """--check's lines for every configured machine, as (level, message):
    reachability, tmux >= 3.2, git, backlog's version, the hook filter's
    parser, the project repository and worktreeLinks sources of each project
    that lists the machine (task-224), tmux control mode, clock skew, and
    each agent CLI's full path, login and version next to the hub's. Two round trips per
    machine: the probes, then the resolved CLIs. Empty with no machines."""
    lines = []
    executables = agent_executables(config)
    hub_versions = {}
    for machine in config.get("machines") or []:
        lines.extend(_doctor_machine(config, machine, executables, hub_versions))
    return lines


def _doctor_machine(config, machine, executables, hub_versions):
    name = machine["name"]
    projects = projects_on(config, name)
    out = []
    if not projects:
        out.append(("WARN", f"machine {name}: no project lists this machine -- "
                            "tick it under Machines on a project's Settings card and save"))
    ignored = {p["name"]: _ignored_check_files(p) for p in projects}
    probes = [
        ("tmux", ["tmux", "-V"]),
        ("git", ["git", "--version"]),
        ("git_name", ["git", "config", "--global", "--get", "user.name"]),
        ("git_email", ["git", "config", "--global", "--get", "user.email"]),
        ("filter", ["sh", "-c", "command -v python3"]),
        ("control", ["sh", "-c", _CONTROL_MODE_PROBE]),
        ("clock", ["date", "+%s.%N"]),
        ("backlog", _resolve_probe("backlog")),
        *((("agent", exe), _resolve_probe(exe)) for exe in executables),
        *((("repo", p["name"]), ["sh", "-c", _REPO_PROBE, "sh",
                                 MachinePath(f"{machine['repoRoot'].rstrip('/')}/{p['name']}"),
                                 MachinePath(f"{machine['repoRoot'].rstrip('/')}/{p['name']}.git")])
          for p in projects),
        *((("link", p["name"], rel), ["test", "-e", links_source_path(machine, p, rel)])
          for p in projects for rel in p.get("worktreeLinks") or []),
        *((("ignored", p["name"], rel), ["test", "-e", links_source_path(machine, p, rel)])
          for p in projects for rel in ignored[p["name"]]),
    ]
    started = time.time()
    results = run_batch(config, name, [(argv, None) for _, argv in probes])
    finished = time.time()
    if results and all(r.status == UNREACHED for r in results):
        return out + [("FAIL", f"machine {name}: cannot reach {machine['ssh']} over ssh with BatchMode "
                         f"({results[0].stderr.strip() or 'no answer'}) -- check the key and host, "
                         f"then run: ssh {machine['ssh']} true")]
    out.append(("PASS", f"machine {name}: {machine['ssh']} reachable over ssh (BatchMode)"))
    by_key = dict(zip((key for key, _ in probes), results))

    def answered(key):
        result = by_key[key]
        if result.status != ANSWERED:
            out.append(("FAIL", f"machine {name}: no answer for the {key if isinstance(key, str) else key[0]} "
                                f"check ({result.stderr.strip() or result.status})"))
            return None
        return result

    result = answered("tmux")
    if result is not None:
        found = _parse_version(result.stdout) if result.returncode == 0 else None
        floor = ".".join(map(str, MIN_TMUX_VERSION))
        if found is None:
            out.append(("FAIL", f"machine {name}: tmux not found -- install tmux {floor} or later"))
        elif found[:2] < MIN_TMUX_VERSION:
            out.append(("FAIL", f"machine {name}: {result.stdout.strip()} is too old -- {floor} or "
                                "later is required for status subscriptions"))
        else:
            out.append(("PASS", f"machine {name}: {result.stdout.strip()} (>= {floor})"))
    result = answered("git")
    if result is not None:
        if result.returncode == 0:
            out.append(("PASS", f"machine {name}: {result.stdout.strip()}"))
        else:
            out.append(("FAIL", f"machine {name}: git not found -- required for task worktrees"))
    identity = [by_key[key] for key in ("git_name", "git_email")]
    if all(r.ok and r.stdout.strip() for r in identity):
        out.append(("PASS", f"machine {name}: git author identity "
                            f"{identity[0].stdout.strip()} <{identity[1].stdout.strip()}>"))
    elif all(r.status == ANSWERED for r in identity):
        missing = [key for key, r in zip(("user.name", "user.email"), identity)
                   if not r.ok or not r.stdout.strip()]
        out.append(("WARN", f"machine {name}: git author identity missing global "
                            f"{', '.join(missing)} -- commits there may use a placeholder; "
                            "set it with git config --global"))
    else:
        out.append(("WARN", f"machine {name}: git author identity could not be checked"))
    result = answered("filter")
    if result is not None:
        if result.returncode == 0 and result.stdout.strip():
            out.append(("PASS", f"machine {name}: python3 at {result.stdout.strip().splitlines()[0]}"))
        else:
            # task-210: jq alone would pass the hook filter, but the orphan
            # sweep End session and cleanup run on the worker is python3.
            out.append(("FAIL", f"machine {name}: python3 not found -- the worker-side orphan "
                                "sweep and the agents' Notification hook filter need it"))
    result = answered("control")
    if result is not None:
        if result.returncode == 0 and "centrale-control-ok" in result.stdout:
            out.append(("PASS", f"machine {name}: tmux control mode works"))
        else:
            detail = (result.stdout or result.stderr).strip().splitlines()
            out.append(("FAIL", f"machine {name}: tmux control mode failed"
                                f"{' (' + detail[-1] + ')' if detail else ''}"))
    result = answered("clock")
    if result is not None:
        try:
            remote = float(result.stdout.strip())
        except ValueError:
            out.append(("FAIL", f"machine {name}: could not read its clock ({result.stdout.strip()!r})"))
        else:
            # The worker read its clock somewhere inside the round trip.
            skew = remote - (started + finished) / 2
            slack = (finished - started) / 2
            if abs(skew) - slack > CLOCK_SKEW_TOLERANCE_SECONDS:
                out.append(("FAIL", f"machine {name}: clock is {skew:+.1f}s off the hub's (tolerance "
                                    f"{CLOCK_SKEW_TOLERANCE_SECONDS:g}s) -- agent status times are "
                                    "compared across machines; enable time sync (NTP) there"))
            else:
                out.append(("PASS", f"machine {name}: clock within {CLOCK_SKEW_TOLERANCE_SECONDS:g}s of "
                                    f"the hub's ({skew:+.2f}s)"))

    resolved = {}
    for key in ["backlog", *(("agent", exe) for exe in executables)]:
        result = answered(key)
        if result is None:
            continue
        executable = "backlog" if key == "backlog" else key[1]
        path = _resolved_path(result.stdout) if result.returncode == 0 else None
        if path is None:
            level = "FAIL" if key == "backlog" else "WARN"
            out.append((level, f"machine {name}: {executable} not found by an interactive shell "
                               f"there (bash -ic 'command -v {executable}')"))
        else:
            resolved[executable] = path

    for project in projects:
        result = answered(("repo", project["name"]))
        if result is None:
            continue
        repo = MachinePath(f"{machine['repoRoot'].rstrip('/')}/{project['name']}")
        if result.returncode == 0 and result.stdout.strip() == "parked":
            out.append(("PASS", f"machine {name}: {project['name']}: parked project repository {repo} present"))
        elif result.returncode == 0 and result.stdout.strip() == "legacy":
            out.append(("FAIL", f"machine {name}: {project['name']}: legacy bare repository {repo}.git -- "
                                "migrate it using docs/operations.md#setting-up-a-worker"))
        else:
            remote = shell_word(repo)
            setup = (f"git init -b parking {remote} && "
                     f"git -C {remote} -c user.name=Parked -c user.email=parked@localhost "
                     "commit --allow-empty -m parked && "
                     f"git -C {remote} checkout --detach HEAD")
            hub = shlex.quote(os.path.expanduser(project["path"]))
            push = (f"git -C {hub} push "
                    f"{shlex.quote(machine['ssh'] + ':' + str(repo) + '/.git')} main:main")
            out.append(("FAIL", f"machine {name}: {project['name']}: no parked project repository at {repo} "
                                f"(detached HEAD required); see docs/operations.md#setting-up-a-worker\n"
                                f"run this on {name}:\n{setup}\n"
                                f"run this on hub:\n{push}"))
        for rel in project.get("worktreeLinks") or []:
            result = answered(("link", project["name"], rel))
            if result is None:
                continue
            source = links_source_path(machine, project, rel)
            if result.returncode == 0:
                out.append(("PASS", f"machine {name}: {project['name']}: worktreeLinks source {source} present"))
            else:
                out.append(("WARN", f"machine {name}: {project['name']}: worktreeLinks source {source} "
                                    f"is missing -- run this on hub:\n{_missing_link_command(machine, project, rel)}"))
        for rel in ignored[project["name"]]:
            result = answered(("ignored", project["name"], rel))
            if result is not None and result.returncode != 0:
                out.append(("WARN", f"machine {name}: {project['name']}: ignored checkCommand file "
                                    f"{links_source_path(machine, project, rel)} is missing -- "
                                    f"run this on hub:\n{_copy_from_hub(machine, project, rel)}"))

    # task-240: doctor reads existing worker main; only spawn may push it.
    for project in projects:
        if project.get("workerCheck"):
            import remote
            out.append(remote.doctor_worker_readiness(config, machine, project))

    out.extend(_doctor_tools(config, name, resolved, hub_versions, projects, machine))
    return out


_TRUST_PROBE = r'''# centrale-trust-probe
import json, os, pathlib, sys, tomllib
repo = os.path.realpath(os.path.expanduser(sys.argv[1]))
kind = sys.argv[2]
home = pathlib.Path.home()
try:
    if kind == "codex":
        path = pathlib.Path(os.environ.get("CODEX_HOME", home / ".codex")) / "config.toml"
        with path.open("rb") as stream:
            projects = tomllib.load(stream).get("projects", {})
        trusted = projects.get(repo, {}).get("trust_level") == "trusted"
    else:
        path = home / ".claude.json"
        with path.open(encoding="utf-8") as stream:
            projects = json.load(stream).get("projects", {})
        trusted = projects.get(repo, {}).get("hasTrustDialogAccepted") is True
except FileNotFoundError:
    trusted = False
except (OSError, ValueError, TypeError, AttributeError):
    print("unknown")
    sys.exit(2)
print("trusted" if trusted else "untrusted")
'''

_CODEX_REVIEW_PROBE = r'''# centrale-codex-review-probe
import os, pathlib, sys, tomllib
path = pathlib.Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser() / "config.toml"
try:
    with path.open("rb") as stream:
        review = tomllib.load(stream).get("approvals_reviewer")
except FileNotFoundError:
    review = None
except (OSError, ValueError, TypeError):
    print("unknown")
    sys.exit(2)
print("auto_review" if review == "auto_review" else "none")
'''


def _doctor_tools(config, name, resolved, hub_versions, projects, machine):
    """Second round trip: each CLI's version, login and project trust,
    plus Codex's no-model sandbox probe and fallback setting (task-229)."""
    if not resolved:
        return []
    keys, commands = [], []
    for executable, path in resolved.items():
        keys.append(("version", executable))
        commands.append(([path, "--version"], None))
        login = LOGIN_PROBES.get(os.path.basename(executable))
        if login:
            keys.append(("login", executable))
            commands.append(([path, *login], None))
        if os.path.basename(executable) == "codex":
            # task-229: the CLI runs its bundled bwrap here, with no model request.
            keys.append(("sandbox", executable))
            commands.append(([path, "sandbox", "true"], None))
            keys.append(("review", executable))
            commands.append((["python3", "-c", _CODEX_REVIEW_PROBE], None))
    for project in projects:
        repo = MachinePath(f"{machine['repoRoot'].rstrip('/')}/{project['name']}")
        for executable in resolved:
            kind = os.path.basename(executable)
            if kind in ("claude", "codex"):
                keys.append(("trust", project["name"], kind))
                commands.append((["python3", "-c", _TRUST_PROBE, repo, kind], None))
    by_key = dict(zip(keys, run_batch(config, name, commands)))
    out = []
    for executable, path in resolved.items():
        result = by_key[("version", executable)]
        found = (server._version_token(result.stdout + " " + result.stderr)
                 if result.status == ANSWERED and result.returncode == 0 else None)
        if executable == "backlog":
            hub = version.TESTED_BACKLOG_VERSION
            hub_label = f"tested against {hub}"
        else:
            if executable not in hub_versions:
                hub_versions[executable] = hub_tool_version(executable)
            hub = hub_versions[executable]
            hub_label = f"hub {hub or 'not found'}"
        login = by_key.get(("login", executable))
        login_label = ""
        if login is not None:
            if login.status != ANSWERED:
                login_label = ", login not checked (no answer)"
            elif login.returncode == 0:
                login_label = ", logged in"
            else:
                out.append(("FAIL", f"machine {name}: {executable} at {path} is not logged in -- "
                                    f"log in there once by hand"))
        if found is None:
            out.append(("WARN", f"machine {name}: {executable} at {path} did not report a version "
                                f"({hub_label})"))
        elif hub and found != hub:
            out.append(("WARN", f"machine {name}: {executable} at {path}, version {found} ({hub_label})"
                                f"{login_label} -- versions differ; this is only a warning"))
        else:
            out.append(("PASS", f"machine {name}: {executable} at {path}, version {found} "
                                f"({hub_label}){login_label}"))
        if os.path.basename(executable) == "codex":
            sandbox = by_key[("sandbox", executable)]
            review = by_key[("review", executable)]
            if sandbox.ok:
                out.append(("PASS", f"machine {name}: codex sandbox can run a command"))
            else:
                detail = (sandbox.stderr or sandbox.stdout).strip().splitlines()
                reason = detail[-1] if detail else _result_reason(sandbox)
                fallback = review.ok and review.stdout.strip() == "auto_review"
                level = "WARN" if fallback else "FAIL"
                explanation = ("commands can run unsandboxed after automatic review"
                               if fallback else "commands may fail")
                out.append((level, f"machine {name}: codex sandbox cannot run a command "
                                   f"({reason}) -- {explanation}; "
                                   "see docs/operations.md#troubleshooting"))
    for project in projects:
        repo = MachinePath(f"{machine['repoRoot'].rstrip('/')}/{project['name']}")
        for executable in resolved:
            kind = os.path.basename(executable)
            if kind not in ("claude", "codex"):
                continue
            result = by_key[("trust", project["name"], kind)]
            if result.ok and result.stdout.strip() == "trusted":
                out.append(("PASS", f"machine {name}: {project['name']}: {kind} trust recorded for {repo}"))
            elif result.ok and result.stdout.strip() == "untrusted":
                cd_path = str(repo)
                if cd_path.startswith("~/"):
                    suffix = cd_path[2:]
                    cd_path = "~/" + suffix if shlex.quote(suffix) == suffix else '"$HOME"/' + shlex.quote(suffix)
                else:
                    cd_path = shlex.quote(cd_path)
                out.append(("FAIL", f"machine {name}: {project['name']}: {kind} trust is not recorded -- "
                                    f"run cd {cd_path} && {kind} on that machine, "
                                    "accept the folder, then quit without sending a prompt"))
            else:
                out.append(("FAIL", f"machine {name}: {project['name']}: could not check {kind} trust "
                                    f"for {repo} ({_result_reason(result)})"))
    return out
