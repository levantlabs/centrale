"""Agent status from remote machines (doc-3, "Agent status without a
tunnel"; task-209).

An agent on a worker cannot reach this server: there is no reverse tunnel.
Its hooks write `<state> <firedAt>` into its own tmux pane option
(@centrale_state, see server.hook_argv's "tmux" sink), and the hub reads
those options over ONE tmux control-mode connection per worker, carried by
the same ssh configuration as every other remote command (machines.py).

tmux 3.4 does not report a subscription to a pane in another session, not
even its initial value (task-205), and Centrale runs one session per task.
So the connection attaches to a dedicated, persistent session on the
worker (CONTROL_SESSION), each task's window is linked into it
(`link-window -d`; the window stays in its task session too) and its pane
is subscribed by pane id. A pane is recognised by its identity options
(@centrale_project, @centrale_task, @centrale_kind, set at spawn by
identity_commands), never by its %N id, which tmux reuses: every value
carries the identity and is checked against it.

Linking and subscriptions are (re)armed by a scan -- one `list-panes -a`
-- on connect, on every reconnect, whenever tmux reports
%sessions-changed, and on rearm() (spawn and resume call it). Every state
change enters server.record_agent_event, the same function the HTTP hook
uses, with the hook's own firedAt; its clock clamp allows the clock skew
`--check` verifies. A codex pane's corroborating captures go over this
connection too (server.remote_pane_capture).

At startup (start()), every worker is connected and read before anything
is announced to orchestrators; a read-back state is the pane's own current
claim, so it is restored as it stands -- `working` included (decision-5,
amended). A worker not read within BOOT_WAIT_SECONDS is announced as
unreachable.

Nothing here runs without `machines` configured: no thread, no ssh.
Python 3.12 stdlib only. Calls into `server` and `machines` go through the
module objects at call time, so tests patch them in one place.
"""

from __future__ import annotations

import collections
import math
import re
import shlex
import subprocess
import threading
import time

import machines
import orchestrator
import server

CONTROL_SESSION = "centrale_control"
IDENTITY_OPTIONS = ("@centrale_project", "@centrale_task", "@centrale_kind")
STATE_OPTION = server.PANE_STATE_OPTION
# One field per identity option and the state; the state comes last since
# it holds a space. A '|' cannot occur in a project name or a task id.
_VALUE_FORMAT = "|".join(f"#{{{option}}}" for option in (*IDENTITY_OPTIONS, STATE_OPTION))
_SCAN_FORMAT = "#{pane_id}|#{window_id}|#{session_name}|" + _VALUE_FORMAT
# Startup waits this long for each worker's first read: ssh's own
# ConnectTimeout, plus the scan itself.
BOOT_WAIT_SECONDS = float(dict(machines.SSH_OPTIONS)["ConnectTimeout"]) + 3.0
COMMAND_TIMEOUT_SECONDS = 10.0
# A safety net under %sessions-changed and rearm(): one list-panes over the
# open connection now and then picks up a pane whose identity options
# landed after the scan its session's creation triggered.
RESCAN_SECONDS = 30.0
RECONNECT_DELAYS = (1.0, 2.0, 5.0)
REACHABLE, UNREACHABLE, CONNECTING = "reachable", "unreachable", "connecting"

_connections = {}
_connections_lock = threading.Lock()


class ControlError(Exception):
    """A control-mode command failed (`%error`) or got no answer."""


def identity_commands(target, project, task_id, kind):
    """The tmux argv lists (without the leading `tmux`) that give the pane
    at `target` its identity options, for a spawn or resume on a worker
    to run right after `new-session`. `kind` is claude, codex or custom."""
    values = (project, str(task_id).upper(), kind or "custom")
    return [["set-option", "-p", "-t", target, option, value]
            for option, value in zip(IDENTITY_OPTIONS, values)]


def control_argv(machine):
    """The ssh command that opens `machine`'s control connection: control
    mode (-C, no terminal needed) attached to CONTROL_SESSION, created
    with a window that does nothing if it is not there yet.

    With -A, -D detaches only the other clients of this session, including
    orphans left by dead ssh connections, on every attach (task-249).
    Agent sessions and their clients are untouched. Do this in the attach
    itself so cleanup cannot accidentally detach the new client."""
    remote = shlex.join([*machines.tmux_argv(machine), "-C", "new-session", "-A", "-D",
                         "-s", CONTROL_SESSION, "sleep infinity"])
    return [*machines.ssh_command(), machine["ssh"], remote]


def open_control(machine):
    """The process boundary: tests replace it with a scripted stand-in.
    Returns an object with Popen's stdin/stdout (text, line-buffered),
    poll(), terminate() and wait()."""
    return subprocess.Popen(control_argv(machine), stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            text=True, bufsize=1, encoding="utf-8", errors="replace")


def _unescape(value):
    """Control mode writes a non-printable byte, and a backslash, as \\ooo."""
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), value)


class _Reply:
    def __init__(self, command):
        self.command = command
        self.done = threading.Event()
        self.lines = []
        self.error = None


class Connection:
    """One worker's control connection, its subscriptions and what it last
    read from each task's pane."""

    def __init__(self, config, machine, booting=True):
        self.config = config
        self.machine = machine
        self.name = machine["name"]
        self.reach = CONNECTING
        self.error = None
        self.first_read = threading.Event()  # set by the first scan, or the first failure
        self._lock = threading.RLock()
        self._write_lock = threading.Lock()
        self._pending = collections.deque()
        self._proc = None
        self._panes = {}        # pane id -> (project, TASK-ID, kind)
        self._captures = {}     # (project, TASK-ID) -> callback owned by this connection
        self._subscribed = set()
        self._last = {}         # (project, TASK-ID) -> last value applied
        self._booting = booting
        self._boot_read = {}    # (project, TASK-ID) -> (state or None, kind)
        self._flags_wanted = True
        self._stop = threading.Event()
        self._scan_wanted = threading.Event()
        self._threads = []

    # -- lifecycle -------------------------------------------------------

    def start(self):
        try:
            for target in (self._run, self._scanner):
                thread = threading.Thread(target=target, name=f"centrale-control-{self.name}",
                                          daemon=True)
                thread.start()
                self._threads.append(thread)
        except (OSError, RuntimeError):
            self.stop()  # a first thread may have started before the second failed
            raise
        return self

    def stop(self):
        with self._lock:
            self._stop.set()
            identities = list(self._captures)
        for identity in identities:
            self._unregister_capture(identity)
        self._scan_wanted.set()
        proc = self._proc
        if proc is not None:
            try:
                proc.terminate()
            except OSError:
                pass

    def rearm(self):
        """Ask for a scan: link and subscribe any task pane not yet armed."""
        self._scan_wanted.set()

    def tasks(self):
        """(project, TASK-ID, kind) of every task pane currently armed."""
        with self._lock:
            return sorted(set(self._panes.values()), key=lambda t: (t[0], t[1]))

    def end_boot(self):
        """Close the startup window; returns what the first read found."""
        with self._lock:
            self._booting = False
            return dict(self._boot_read)

    def _run(self):
        attempt = 0
        while not self._stop.is_set():
            try:
                proc = open_control(self.machine)
            except OSError as exc:
                proc, error = None, str(exc)
            if proc is not None:
                self._proc = proc
                self._flags_wanted = True
                self._scan_wanted.set()
                error = self._read(proc)
                attempt = 0 if self.reach == REACHABLE else attempt
            self._disconnected(error)
            if self._stop.is_set():
                return
            self._stop.wait(RECONNECT_DELAYS[min(attempt, len(RECONNECT_DELAYS) - 1)])
            attempt += 1

    def _disconnected(self, error):
        with self._lock:
            self._proc = None
            self.reach = UNREACHABLE
            self.error = error or "the control connection ended"
            # The worker's tmux dropped this client's subscriptions with it.
            identities = set(self._panes.values())
            self._subscribed.clear()
            self._panes.clear()
        # A disconnected pane may be gone by the next scan, which no longer
        # has its old subscription to unregister (task-230).
        for identity in identities:
            self._unregister_capture(identity)
        self._fail_pending(self.error)
        self.first_read.set()

    def _fail_pending(self, error):
        with self._write_lock:
            while self._pending:
                reply = self._pending.popleft()
                reply.error = error
                reply.done.set()

    # -- the protocol ------------------------------------------------------

    def command(self, text, timeout=COMMAND_TIMEOUT_SECONDS):
        """Send one control-mode command; return its output lines. Raises
        ControlError on `%error`, no connection, or no answer in time."""
        reply = _Reply(text)
        with self._write_lock:
            proc = self._proc
            if proc is None:
                raise ControlError(f"{self.name}: not connected")
            self._pending.append(reply)
            try:
                proc.stdin.write(text + "\n")
                proc.stdin.flush()
            except (OSError, ValueError) as exc:
                self._pending.remove(reply)
                raise ControlError(f"{self.name}: {exc}") from None
        if not reply.done.wait(timeout):
            raise ControlError(f"{self.name}: no answer to {text.split()[0]} within {timeout:g}s")
        if reply.error is not None:
            raise ControlError(reply.error)
        return reply.lines

    def _read(self, proc):
        """Read until EOF; returns why the connection ended."""
        block = None  # (command number, lines) while inside a %begin block
        ours = False
        try:
            for raw in proc.stdout:
                line = raw.rstrip("\n")
                if block is not None:
                    end = re.match(r"%(end|error) \d+ (\d+) \d+$", line)
                    if end and end.group(2) == block[0]:
                        if ours:
                            self._answer(block[1], line.startswith("%error"))
                        block = None
                    else:
                        block[1].append(line)
                    continue
                begin = re.match(r"%begin \d+ (\d+) (\d+)$", line)
                if begin:
                    # flags 0: the attach command tmux ran at startup.
                    block, ours = (begin.group(1), []), begin.group(2) != "0"
                elif line.startswith("%subscription-changed "):
                    self._subscription_changed(line)
                elif line.startswith("%sessions-changed"):
                    self._scan_wanted.set()
                elif line.startswith("%exit"):
                    break
        except (OSError, ValueError) as exc:
            return str(exc)
        finally:
            try:
                proc.terminate()
            except OSError:
                pass
        return "the control connection ended"

    def _answer(self, lines, failed):
        with self._write_lock:
            reply = self._pending.popleft() if self._pending else None
        if reply is None:
            return
        if failed:
            reply.error = f"{self.name}: {' '.join(lines).strip() or reply.command + ' failed'}"
        reply.lines = lines
        reply.done.set()

    # -- scans: link, subscribe, read ---------------------------------------

    def _scanner(self):
        while not self._stop.is_set():
            self._scan_wanted.wait(RESCAN_SECONDS)
            if self._stop.is_set():
                return
            self._scan_wanted.clear()
            if self._proc is None:
                continue
            try:
                self.scan()
            except ControlError as exc:
                with self._lock:
                    self.error = str(exc)
            finally:
                self.first_read.set()

    def scan(self):
        """One list-panes -a: link every task window not yet linked into
        CONTROL_SESSION, subscribe every task pane not yet subscribed, drop
        the panes that are gone, and apply each pane's current state."""
        if self._flags_wanted:
            # Output is never needed (captures are asked for), and this
            # client's size must never resize a task's window.
            self.command("refresh-client -f no-output,ignore-size")
            self._flags_wanted = False
        rows = self.command(f"list-panes -a -F '{_SCAN_FORMAT}'")
        linked, found = set(), {}
        for row in rows:
            fields = row.split("|", 6)
            if len(fields) != 7:
                continue
            pane, window, session, project, task, kind, value = fields
            if session == CONTROL_SESSION:
                linked.add(window)
                continue
            identity = self._identity(project, task, kind)
            if identity is not None:
                found[pane] = (window, identity, _unescape(value))
        for pane, (window, identity, value) in found.items():
            try:
                if window not in linked:
                    self.command(f"link-window -d -s {window} -t {CONTROL_SESSION}:")
                    linked.add(window)
                if pane not in self._subscribed:
                    self.command(f"refresh-client -B 'centrale{pane[1:]}:{pane}:{_VALUE_FORMAT}'")
                    with self._lock:
                        self._subscribed.add(pane)
            except ControlError:
                if self._proc is None:
                    raise
                continue  # the pane went away since list-panes; the next scan drops it
            with self._lock:
                self._panes[pane] = identity
            self._apply(pane, identity, value)
        for pane in list(self._subscribed - set(found)):
            try:
                self.command(f"refresh-client -B 'centrale{pane[1:]}'")
            except ControlError:
                pass
            with self._lock:
                self._subscribed.discard(pane)
                identity = self._panes.pop(pane, None)
            if identity is not None:
                self._unregister_capture(identity)
        with self._lock:
            self.reach, self.error = REACHABLE, None
        self.first_read.set()

    def _identity(self, project, task, kind):
        """(project, TASK-ID, kind) for a pane claiming a task of a project
        that may run on THIS machine (task-224: one listing it in its
        `machines`), else None: a pane cannot speak for a project that
        never runs here, nor for a malformed task id."""
        if not project or not server.TASK_ID_RE.fullmatch(task or ""):
            return None
        if not any(p.get("name") == project and self.name in (p.get("machines") or [])
                   for p in self.config.get("projects") or []):
            return None
        return project, task.upper(), kind if kind in server.AGENT_KINDS else None

    def _subscription_changed(self, line):
        match = re.match(r"%subscription-changed \S+ \S+ \S+ \S+ (%\d+) : ?(.*)$", line)
        if not match:
            return
        pane, value = match.group(1), _unescape(match.group(2))
        with self._lock:
            known = self._panes.get(pane)
        fields = value.split("|", 3)
        if len(fields) != 4:
            return
        identity = self._identity(*fields[:3])
        if known is None or identity != known:
            # A reused pane id, or a pane this connection has not scanned.
            self._scan_wanted.set()
            return
        self._apply(pane, identity, fields[3])

    def _apply(self, pane, identity, value):
        project, task_id, kind = identity
        key = (project, task_id)
        self._register_capture(pane, identity)
        state, fired_at = _parse_state(value)
        with self._lock:
            last = self._last.get(key)
            if last == value:
                return
            if state is not None and (_parse_state(last)[1] or 0) > fired_at:
                # A scan's read that a subscription has already overtaken.
                return
            self._last[key] = value
            if self._booting:
                self._boot_read[key] = (state, kind)
                if state is not None:
                    server.restore_remote_state(project, task_id, state, kind, fired_at)
                return
        if state is not None:
            server.record_agent_event(project, task_id, state, agent_kind=kind, fired_at=fired_at,
                                      clock_skew=machines.CLOCK_SKEW_TOLERANCE_SECONDS)

    # -- codex corroboration ---------------------------------------------

    def _register_capture(self, pane, identity):
        key = identity[:2]
        with self._lock:
            if self._stop.is_set():
                return
            capture = lambda lines: self.capture(pane, lines)
            self._captures[key] = capture
            server.remote_pane_capture[key] = capture
            server.remote_task_pane_capture[(self.name, *key)] = capture

    def _unregister_capture(self, identity):
        key = identity[:2]
        with self._lock:
            capture = self._captures.pop(key, None)
            if capture is None:
                return
            if server.remote_task_pane_capture.get((self.name, *key)) is capture:
                server.remote_task_pane_capture.pop((self.name, *key), None)
            if server.remote_pane_capture.get(key) is capture:
                server.remote_pane_capture.pop(key, None)

    def capture(self, pane, max_lines):
        """capture_session_pane's contract, over this connection."""
        try:
            lines = self.command(f"capture-pane -p -t {pane} -S -{int(max_lines)}")
        except ControlError as exc:
            missing = "can't find" in str(exc).lower()
            raise server.PaneCaptureError(str(exc), status=404 if missing else 503) from None
        lines = list(lines)
        while lines and not lines[-1].strip():
            lines.pop()
        return lines[-max_lines:]


def _parse_state(value):
    """(state, firedAt) from `<state> <firedAt>`, or (None, None) for an
    empty, malformed or not allow-listed value."""
    parts = (value or "").split(" ")
    if len(parts) != 2 or parts[0] not in server.AGENT_STATES:
        return None, None
    try:
        fired_at = float(parts[1])
    except ValueError:
        return None, None
    if not math.isfinite(fired_at):
        return None, None
    return parts[0], fired_at


# ---------------------------------------------------------------------------
# The hub's connections
# ---------------------------------------------------------------------------

def _clear_machine_captures(name):
    """Drop callbacks still registered for a retired machine, including
    ones a stand-in or a failed connection did not unregister itself."""
    for key, capture in list(server.remote_task_pane_capture.items()):
        if key[0] != name:
            continue
        server.remote_task_pane_capture.pop(key, None)
        if server.remote_pane_capture.get(key[1:]) is capture:
            server.remote_pane_capture.pop(key[1:], None)


def reconcile(config):
    """task-228: apply a successful Settings machine save to the live
    control connections. Unchanged definitions keep their subscriptions;
    changed definitions get a fresh non-boot connection so its first scan
    reads current pane state, and removed definitions release captures."""
    desired = {m["name"]: m for m in config.get("machines") or []}
    warnings = []
    with _connections_lock:
        for name, connection in list(_connections.items()):
            if name in desired and connection.machine == desired[name]:
                continue
            connection.stop()
            _clear_machine_captures(name)
            _connections.pop(name, None)
        for name, machine in desired.items():
            if name not in _connections:
                try:
                    _connections[name] = Connection(config, machine, booting=False).start()
                except (OSError, RuntimeError) as exc:
                    warnings.append(f"machine {name}: live status connection could not start ({exc}) -- "
                                    "restart Centrale after fixing it")
    return warnings

def start(config, wait=BOOT_WAIT_SECONDS):
    """Connect to every configured machine and read every task pane before
    returning (each within `wait` seconds). Returns the boot report
    announce() publishes: [{"name", "reach", "error", "panes": {key:
    (state, kind)}}]. Empty, and nothing started, without machines."""
    if not machines.configured(config):
        return []
    with _connections_lock:
        for machine in config["machines"]:
            if machine["name"] not in _connections:
                _connections[machine["name"]] = Connection(config, machine).start()
        connections = [_connections[m["name"]] for m in config["machines"]]
    deadline = time.monotonic() + wait
    for connection in connections:
        connection.first_read.wait(max(0.0, deadline - time.monotonic()))
    report = []
    for connection in connections:
        panes = connection.end_boot()
        with connection._lock:
            reach = connection.reach if connection.reach == REACHABLE else UNREACHABLE
            error = connection.error or (None if reach == REACHABLE else
                                         f"not reached within {wait:g}s")
        report.append({"name": connection.name, "reach": reach, "error": error, "panes": panes,
                       "lastSeenLive": ([] if reach == REACHABLE else
                                        _journal_live_tasks(config, connection.name))})
    _reconcile_boot_sessions(config, report)
    return report


def _reconcile_boot_sessions(config, report):
    """Retire worker sessions absent from a successful first pane read.

    Older journals did not store the machine. Their read-only location is
    enough to classify them without asking an unreachable worker to prove
    that a session ended (task-237).
    """
    projects = {p["name"]: p for p in config.get("projects") or []}
    workers = {entry["name"]: entry for entry in report}
    pane_machines = {key: entry["name"] for entry in report if entry["reach"] == REACHABLE
                     for key in entry["panes"]}
    journal = server.fleet_history
    with journal.lock:
        tracked = list(journal.sessions.items())
    for (project_name, task_id), session in tracked:
        machine = pane_machines.get((project_name, task_id)) or session.get("machine")
        project = projects.get(project_name)
        if not machine and project and project.get("machines"):
            location = machines.locate(config, project, task_id, live=False)
            if not location.conflict:
                machine = location.machine
        if machine and machine != session.get("machine"):
            with journal.lock:
                if (project_name, task_id) in journal.sessions:
                    journal.sessions[(project_name, task_id)]["machine"] = machine
        worker = workers.get(machine)
        if worker and worker["reach"] == REACHABLE and (project_name, task_id) not in worker["panes"]:
            server.record_session_ended(project_name, task_id)


def announce(config, report):
    """Publish one orchestrator-wait line per remote task at boot, after
    announce_restart_states' local lines: its read-back state, or that
    its worker was not reached. Which tasks an unreached worker holds is
    not knowable from here, so those lines go to the tasks the fleet
    journal last saw live on a project of that machine, as start() found
    them before anything else at startup touched the journal."""
    for machine in report:
        name = machine["name"]
        if machine["reach"] == REACHABLE:
            for (project, task_id), (state, kind) in machine["panes"].items():
                public = "idle" if kind == "codex" and state == "finished" else state
                if public is None:
                    message = (f"state unknown after the server restart (no hook event on {name} "
                               "since the session started)")
                else:
                    message = (server.AGENT_STATE_MESSAGES.get(public) or public) + (
                        f" (read back from {name} after the server restart)")
                orchestrator.publish(project, task_id, message)
            continue
        reason = " ".join(str(machine.get("error") or "").split())
        message = f"{name} unreachable after the server restart; state unknown" + (
            f" ({reason})" if reason else "")
        for project, task_id in machine.get("lastSeenLive") or []:
            orchestrator.publish(project, task_id, message)


def _journal_live_tasks(config, machine_name):
    """The tasks the fleet journal last saw live on `machine_name`.

    Use the recorded machine when present; old rows fall back to the hub's
    fetched refs through a read-only location lookup (task-224/task-237).
    """
    projects = {p["name"]: p for p in machines.projects_on(config, machine_name)}
    journal = server.fleet_history
    with journal.lock:
        keys = {(r["project"], r["taskId"]) for r in journal.rows if r["project"] in projects}
        live = sorted(key for key in keys
                      if journal.last_agent_state(*key).get("state") not in (None, "session ended"))
        known_machines = {key: (journal.sessions.get(key) or {}).get("machine") for key in live}
    return [(project, task_id) for project, task_id in live
            if (known_machines[(project, task_id)] or
                machines.locate(config, projects[project], task_id, live=False).machine) == machine_name]


def session_rows(config):
    """The live sessions on every configured machine, shaped like
    server.list_sessions rows plus "machine", "worktree" and "reach"
    (task-210). A reachable machine's rows are its armed task panes; one
    that is not reachable has no live read, so its rows are the tasks the
    fleet journal last saw live there, marked "unreachable" -- unknown
    would claim a state we never read. Empty without machines."""
    import spawn  # local import: avoids a circular import at module load
    import remote

    rows = []
    for machine in config.get("machines") or []:
        name = machine["name"]
        connection = _connections.get(name)
        if connection is not None and connection.reach == REACHABLE:
            reach, tasks = REACHABLE, connection.tasks()
        else:
            reach = UNREACHABLE
            tasks = [(p, t.upper(), None) for p, t in _journal_live_tasks(config, name)]
        for project, task_id, kind in tasks:
            rows.append({"name": spawn.session_name(project, task_id.lower()),
                         "created": None, "attached": False, "machine": name,
                         "reach": reach,
                         "attach": remote.attach_command(machine, spawn.session_name(project, task_id.lower())),
                         "worktree": str(remote.worktree_path(machine, project, task_id))})
    return rows


def rearm(config, machine_name):
    """Re-link and re-subscribe on `machine_name` (spawn and resume call
    this after starting a session there). A no-op for the hub or before
    start()."""
    connection = _connections.get(machine_name)
    if connection is not None:
        connection.rearm()


def reach(machine_name):
    """reachable, unreachable or connecting; None for no connection."""
    connection = _connections.get(machine_name)
    return None if connection is None else connection.reach


def stop_all():
    with _connections_lock:
        for name, connection in list(_connections.items()):
            connection.stop()
            _clear_machine_captures(name)
        _connections.clear()
