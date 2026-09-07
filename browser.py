"""Per-project `backlog browser` launcher for `POST /api/browser`.

Imported lazily by server.py (inside the request handler, or once at
startup for the boot sweep) to avoid a circular import, since this module
imports `server` for its subprocess boundary function
(`launch_browser_process`) and its port/process boundaries
(`port_is_free`, `resolve_listener_pid`, `process_cmdline`,
`kill_process`).

Python 3.12 stdlib only. All calls into `server` go through the module
object at call time (`server.launch_browser_process(...)`, not `from
server import launch_browser_process`) so tests can patch
`server.launch_browser_process` / `server.port_is_free` / etc. in one
place and have both modules see the patch.
"""

from __future__ import annotations

import atexit
import json
import os
import sys
import tempfile
import threading
import time

import server

_state = {}  # project name -> {"proc": Popen-like, "port": int}
_lock = threading.Lock()
_atexit_registered = False

# How many ports forward of the assigned one to try before giving up when
# it's occupied by something we didn't launch.
PORT_WALK_LIMIT = 50

# How long, and how often, to poll for a just-launched child to actually
# start listening before trusting the URL we're about to hand out.
LAUNCH_VERIFY_TIMEOUT = 2.0
LAUNCH_VERIFY_INTERVAL = 0.1

# A launched child's cmdline (see server.process_cmdline) must contain
# both of these for the boot sweep to trust it's still the same `backlog
# browser` process a registry entry recorded, not some unrelated process
# that has since reused the same pid.
ORPHAN_CMDLINE_MARKERS = ("backlog", "browser")


class BrowserError(Exception):
    """Raised for any /api/browser failure. `status` is the HTTP status
    the server should respond with (400/404 validation, 500 for a launch
    failure)."""

    def __init__(self, message, status=400):
        super().__init__(message)
        self.message = message
        self.status = status

    def __str__(self):
        return self.message


def _reset_state():
    """Test helper: clear tracked browser processes without touching any
    real process."""
    with _lock:
        _state.clear()


def _is_alive(proc):
    try:
        return proc.poll() is None
    except Exception:
        return False


def _port_for(config, project, index):
    port = project.get("browserPort")
    if isinstance(port, int):
        return port
    base = config.get("browserPortBase", server.DEFAULT_BROWSER_PORT_BASE)
    return base + index


def _still_looks_like_a_browser(pid):
    """Whether `pid` is alive AND its cmdline still actually looks like a
    `backlog browser` process (ORPHAN_CMDLINE_MARKERS) -- the one guard
    every pid-keyed decision in this module goes through, so none of them
    ever acts on a pid alone: pids get reused, and a pid that has been
    is neither a process to kill (_kill_if_still_matches) nor a registry
    row to keep (_entry_is_dead). `None` (an unresolved listener pid) is
    a safe, quiet False; so is a pid server.process_cmdline() cannot
    read, which is what keeps the sweep fail-closed."""
    if pid is None:
        return False
    cmdline = server.process_cmdline(pid)
    return bool(cmdline) and all(marker in cmdline for marker in ORPHAN_CMDLINE_MARKERS)


def _kill_if_still_matches(pid):
    """Guarded kill: `pid` is only signaled if _still_looks_like_a_browser
    confirms it. Used for BOTH the wrapper pid and the listener pid in
    sweep_orphaned_browsers, and for the listener pid in _cleanup_all
    (the wrapper pid there already has a live Popen handle to terminate
    directly, which doesn't need this guard -- see _cleanup_all).
    Returns True if a kill was actually sent."""
    if not _still_looks_like_a_browser(pid):
        return False
    server.kill_process(pid)
    return True


def _cleanup_all():
    """atexit hook: best-effort terminate every browser process this
    server launched -- both the wrapper (via its live Popen handle) and,
    if resolved at launch time, the real listener process underneath it
    (see launch_or_reuse/server.resolve_listener_pid) -- and drop their
    registry entries (see _register_launch below) since they're being
    cleanly shut down, not orphaned. Never raises. Only runs on a clean
    interpreter shutdown -- SIGTERM needs
    server.install_terminate_handlers() to get here at all (see its
    docstring); a SIGKILL still skips this entirely, same as any atexit
    hook -- that gap is exactly what sweep_orphaned_browsers() heals on
    the next boot."""
    with _lock:
        procs = list(_state.values())
    for s in procs:
        try:
            s["proc"].terminate()
        except Exception:
            pass
        try:
            _kill_if_still_matches(s.get("listenerPid"))
        except Exception:
            pass
        try:
            _unregister(s["proc"].pid)
        except Exception:
            pass


def _ensure_atexit_registered():
    global _atexit_registered
    if not _atexit_registered:
        atexit.register(_cleanup_all)
        _atexit_registered = True


def _find_project(config, project_name):
    for index, project in enumerate(config.get("projects", [])):
        if project.get("name") == project_name:
            return index, project
    return None, None


# ---------------------------------------------------------------------------
# Process registry: infra state (pid/port/project/start time of every
# `backlog browser` child this or a previous Centrale process launched),
# not task state -- lives in the user's cache directory, not anywhere
# under a project's own repo, so the "Centrale stores no task data of its
# own" rule stays intact. Its only purpose is letting a *future* Centrale
# process's boot sweep clean up after a SIGKILLed one (see
# sweep_orphaned_browsers): a normal exit or clean SIGTERM already
# unregisters its own children via _cleanup_all above.
# ---------------------------------------------------------------------------

REGISTRY_FILENAME = "browsers.json"
REGISTRY_DIRNAME = "centrale"


def registry_path():
    """~/.cache/centrale/browsers.json, or $XDG_CACHE_HOME/centrale/...
    if that's set. A function, not a module-level constant, so tests can
    monkeypatch it to a throwaway path without ever touching a real
    one."""
    cache_home = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    return os.path.join(cache_home, REGISTRY_DIRNAME, REGISTRY_FILENAME)


def _read_registry(path):
    """Never raises: a missing, corrupt, or malformed registry file just
    means there's nothing to sweep."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(data, list):
        return []
    return [e for e in data if isinstance(e, dict) and isinstance(e.get("pid"), int)]


def _write_registry(path, entries):
    """Atomic write (temp file + os.replace), the same style as
    settings.py's projects.json rewrite. Never raises -- a failure to
    persist just means a future boot sweep might not learn about this
    particular child; it never blocks a launch or a sweep."""
    try:
        directory = os.path.dirname(path) or "."
        os.makedirs(directory, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(prefix=".browsers-", suffix=".json.tmp", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(entries, f, indent=2)
                f.write("\n")
            os.replace(tmp_path, path)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
    except OSError:
        pass


def _entry_is_dead(entry):
    """Whether a registry row describes a `backlog browser` that is gone:
    NEITHER of the pids it carries (the wrapper, and the real listener
    when one was resolved) still looks like one, per the same
    _still_looks_like_a_browser guard the boot sweep decides by. A row
    whose pid has since been reused by an unrelated process reads as
    dead here -- correctly, since the browser it recorded is gone -- and
    nothing is ever signaled on the strength of this: dropping a row
    kills nothing (see _register_launch)."""
    return not (
        _still_looks_like_a_browser(entry.get("pid"))
        or _still_looks_like_a_browser(entry.get("listenerPid"))
    )


def _register_launch(project_name, pid, port, listener_pid=None):
    """`pid` is the wrapper process's own pid (the one launch_browser_process's
    Popen handle tracks); `listener_pid` -- the pid actually holding the
    port, resolved via server.resolve_listener_pid -- is recorded
    alongside it when available (None if it couldn't be resolved, in
    which case cleanup/sweep only ever act on the wrapper pid, same as
    before this existed).

    Reconciles before appending (task-157): any row already recorded for
    THIS project whose process is gone (_entry_is_dead) is dropped, so
    relaunching a project's board in one session leaves one row for it
    rather than a dead one beside the live one. The boot sweep would
    have pruned it on the next restart anyway; until then the registry
    was misrepresenting what is running, and it is the same file the
    sweep later trusts to decide what to kill. Rows for OTHER projects
    are never touched here, and a row for this project whose process is
    still alive is KEPT -- two live boards for one project is a shape
    worth carrying into the sweep, not one worth forgetting. Dropping a
    row never signals anything; only the sweep kills.
    """
    path = registry_path()
    entries = [
        e for e in _read_registry(path)
        if e.get("project") != project_name or not _entry_is_dead(e)
    ]
    entries.append({
        "pid": pid,
        "listenerPid": listener_pid,
        "port": port,
        "project": project_name,
        "startTime": time.time(),
    })
    _write_registry(path, entries)


def _unregister(pid):
    path = registry_path()
    entries = [e for e in _read_registry(path) if e.get("pid") != pid]
    _write_registry(path, entries)


def sweep_orphaned_browsers():
    """Called once at server startup (see server.main()): reads whatever
    the registry has left over from a previous Centrale process (most
    plausibly one that was SIGKILLed -- a normal exit or a clean SIGTERM
    already cleared its own entries via _cleanup_all), and for each
    entry, independently guards and kills both pids it may carry -- the
    wrapper (`pid`) and, if it was resolved at launch time, the real
    listener underneath it (`listenerPid`) -- via _kill_if_still_matches:

    - a pid that isn't alive/readable at all (server.process_cmdline
      returns None) is simply stale: nothing to kill for it.
    - a pid that's alive but whose cmdline no longer looks like a
      `backlog browser` process (ORPHAN_CMDLINE_MARKERS) has been reused
      by some unrelated process since this entry was written -- never
      kill on pid alone.
    - only a pid that's alive AND still matches is actually killed
      (server.kill_process).

    An entry counts as "killed" if either of its pids was actually
    killed (the common case for an old entry with no listenerPid: only
    the wrapper pid is checked, same behavior as before this existed);
    it's "stale" only when neither pid needed killing. Every entry is
    removed from the registry by the end either way -- a boot sweep
    processes a snapshot exactly once; from here on this server's own
    launches populate the registry fresh (see _register_launch). Returns
    {"killed": [...], "stale": [...]}, each a list of the registry
    entries in that bucket, for the caller's one-line startup log. Never
    raises.
    """
    path = registry_path()
    entries = _read_registry(path)
    killed = []
    stale = []
    for entry in entries:
        wrapper_killed = _kill_if_still_matches(entry.get("pid"))
        listener_killed = _kill_if_still_matches(entry.get("listenerPid"))
        if wrapper_killed or listener_killed:
            killed.append(entry)
        else:
            stale.append(entry)
    _write_registry(path, [])
    return {"killed": killed, "stale": stale}


def _find_free_port(start_port):
    """Walks forward from start_port (inclusive), bind-checking each via
    server.port_is_free, and returns the first free one. Gives up after
    PORT_WALK_LIMIT ports and returns None -- callers turn that into a
    BrowserError rather than looping forever."""
    for offset in range(PORT_WALK_LIMIT):
        candidate = start_port + offset
        if server.port_is_free(candidate):
            return candidate
    return None


def _verify_launch(proc, port, sleep_fn):
    """Confirms a just-launched child is actually the one now serving on
    `port`, rather than trusting a live Popen handle alone (a process can
    be alive without ever having bound anything -- e.g. it's still
    starting up, crashed after forking, or is hung waiting on stdin
    despite --non-interactive). Polls briefly (LAUNCH_VERIFY_TIMEOUT /
    LAUNCH_VERIFY_INTERVAL, sleep_fn injectable for tests) for the port
    to stop being free (something bound it) while the process is still
    alive. Raises BrowserError with a clear reason on either a dead
    child or a timeout with nothing ever listening; never returns
    anything -- callers only care whether it raised.
    """
    deadline_ticks = max(1, int(LAUNCH_VERIFY_TIMEOUT / LAUNCH_VERIFY_INTERVAL))
    for _ in range(deadline_ticks):
        if not _is_alive(proc):
            raise BrowserError(
                f"backlog browser exited immediately after launch (port {port})", status=500
            )
        if not server.port_is_free(port):
            return  # something's listening -- almost certainly our child
        sleep_fn(LAUNCH_VERIFY_INTERVAL)

    if not _is_alive(proc):
        raise BrowserError(
            f"backlog browser exited immediately after launch (port {port})", status=500
        )
    try:
        proc.terminate()
    except Exception:
        pass
    raise BrowserError(
        f"backlog browser did not start listening on port {port} within {LAUNCH_VERIFY_TIMEOUT}s",
        status=500,
    )


def launch_or_reuse(config, project_name, sleep_fn=None):
    """Launch `backlog browser` for project_name if it isn't already
    running, or reuse a live one. Assigns a stable port from
    `config['browserPortBase']` (default 6421) plus the project's index,
    overridable per-project via `browserPort`.

    Before launching, bind-checks the assigned port (server.port_is_free):
    if something we don't already have a live, tracked process for is
    squatting it (a foreign process, or an orphan from an earlier server
    run that a SIGTERM once skipped cleaning up -- see
    server.install_terminate_handlers), that URL is never handed out.
    Instead, this walks forward to the next free port (_find_free_port)
    and launches there explicitly, *without* --non-interactive: that flag
    is exactly what let this bug's root cause happen in the first place
    (backlog silently rebinding elsewhere on a taken port instead of
    failing), so a walked-forward launch drops it deliberately -- a race
    that beats even the fallback port should fail loudly, not drift to
    yet another port we didn't verify.

    After launching (either path), the URL still isn't returned until
    _verify_launch confirms the child is actually alive and listening.

    Returns {"url": "http://127.0.0.1:<port>"}. Raises BrowserError(404)
    for an unknown project, BrowserError(500) if the process can't start,
    the port range is exhausted, or launch verification fails.
    """
    sleep_fn = sleep_fn or time.sleep

    if not isinstance(project_name, str) or not project_name:
        raise BrowserError("missing or invalid project", status=400)

    index, project = _find_project(config, project_name)
    if project is None:
        raise BrowserError(f"unknown project: {project_name}", status=404)

    assigned_port = _port_for(config, project, index)

    with _lock:
        state = _state.get(project_name)
        if state is not None and state["port"] == assigned_port and _is_alive(state["proc"]):
            return {"url": f"http://127.0.0.1:{state['port']}"}

        if server.port_is_free(assigned_port):
            port = assigned_port
            non_interactive = True
        else:
            # Already know assigned_port itself is occupied -- start
            # walking from the next one, not a redundant re-check of it.
            port = _find_free_port(assigned_port + 1)
            if port is None:
                raise BrowserError(
                    f"no free port found for {project_name} in "
                    f"{assigned_port}-{assigned_port + PORT_WALK_LIMIT}",
                    status=500,
                )
            non_interactive = False

        cmd = ["backlog", "browser", "--port", str(port), "--no-open"]
        if non_interactive:
            cmd.append("--non-interactive")

        try:
            proc = server.launch_browser_process(cmd, cwd=project["path"])
        except OSError as exc:
            raise BrowserError(f"failed to launch backlog browser: {exc}", status=500) from exc

        _verify_launch(proc, port, sleep_fn)

        # `proc.pid` is the node wrapper's own pid -- `backlog browser`
        # immediately forks the real listening server, which reparents
        # away within about a second, so killing only the wrapper can
        # leave that real server running forever (see
        # server.resolve_listener_pid and _kill_if_still_matches). Best-
        # effort: a launch still succeeds either way, just with cleanup
        # only able to track the wrapper pid, exactly as before this
        # existed.
        listener_pid = server.resolve_listener_pid(port)
        if listener_pid is None:
            print(
                f"Centrale: could not resolve the real listening pid for {project_name}'s "
                f"backlog browser on port {port} -- cleanup will only track its wrapper process.",
                file=sys.stderr,
            )

        _state[project_name] = {"proc": proc, "port": port, "listenerPid": listener_pid}
        _register_launch(project_name, proc.pid, port, listener_pid=listener_pid)

    _ensure_atexit_registered()
    return {"url": f"http://127.0.0.1:{port}"}


# ---------------------------------------------------------------------------
# task-157: the board a user just opened may be running an older backlog
# than the one on PATH.
# ---------------------------------------------------------------------------


def version_drift(project_name):
    """What a board this process is tracking for `project_name` reports
    on /api/version, when that differs from the `backlog` on PATH:
    `{"running": ..., "cli": ...}`. None for everything else.

    A running `backlog browser` keeps the version it started with --
    upgrading the package on disk does not touch it -- and after a
    1.50.1 -> 1.51.0 upgrade three boards Centrale had launched sat
    answering 1.50.1 while the owner's terminal said 1.51.0, with
    nothing on screen to explain the disagreement. This is the
    comparison, made where the user opens boards (server._handle_browser
    calls it right after launch_or_reuse, so the answer describes the
    board they are about to look at).

    Reports only; nothing here kills or restarts anything. Centrale
    launched these processes but the user may be reading one, so the
    fix -- stop that board and open it again -- stays theirs to make.

    Any doubt is silence, the same way server.code_drift() degrades: no
    tracked board for the project, a board that does not answer, a
    `backlog` that cannot be asked, or two answers that agree all return
    None. A false "your board is stale" would teach the reader to ignore
    the true one.
    """
    with _lock:
        state = _state.get(project_name)
    if state is None:
        return None
    running = server.probe_browser_version(state["port"])
    if not running:
        return None
    cli = server.backlog_cli_version()
    if not cli or cli == running:
        return None
    return {"running": running, "cli": cli}
