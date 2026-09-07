"""Shared sandbox utilities for the integration test tier.

Every module in tests_integration/ exercises real `git`, `tmux`, and
`backlog` CLI processes -- never the mocked subprocess boundaries the
hermetic suite under tests/ uses -- so the sandboxing rules here are load
bearing, not decorative:

- All repos are fresh temp directories created with `git init` +
  `backlog init --defaults`, never the user's real repos.
- All tmux traffic is redirected to a dedicated socket named uniquely
  per test-run process, never the user's default tmux server and never
  a socket another run is using (see `TMUX_SOCKET` / `make_tmux_shim` /
  `sandboxed_env` / `tmux()` below).
- All ports are ephemeral, chosen by binding to port 0 and releasing
  (`free_port()`), and the reserved 6420-6430 range plus the live
  server's 7420 are never handed out.
- Spawned "agents" are always a harmless `sh -c "sleep ..."` probe via
  `CENTRALE_SPAWN_CMD`-style overrides -- never a real `claude`/`codex`.
- Every test cleans up its own processes and temp dirs via addCleanup,
  even on failure; see `IntegrationCase`.

See tests_integration/README.md for the full contract and how to run
this tier.
"""

from __future__ import annotations

import contextlib
import os
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

CENTRALE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if CENTRALE_ROOT not in sys.path:
    sys.path.insert(0, CENTRALE_ROOT)

# task-161: the dedicated tmux socket is namespaced PER TEST-RUN
# PROCESS, not a fixed name. It used to be the constant
# "centrale-itest", which meant two concurrent runs -- a maintainer's
# `discover tests_integration`, an agent's, and the copy scripts/
# release.sh runs against the staged snapshot -- shared one tmux server:
# either could `kill-server` the other's sessions out from under it, or
# see them in a `list-sessions`. A release-audit run lost all three
# attached-client geometry cases while other agents were testing --
# circumstantial on its own, but the mechanism reproduces on demand:
# pin two concurrent `discover tests_integration` runs to one socket
# (CENTRALE_ITEST_TMUX_SOCKET below) and both fail in the tests that
# read session state. AGENTS.md is explicit about this too: a tier
# launching real processes must namespace every external-resource axis,
# and names tmux -L as one. The socket name carries the pid so a
# leftover file is traceable to the run that made it, plus random bytes
# because pids are reused.
#
# CENTRALE_ITEST_TMUX_SOCKET overrides it, for driving a run's socket
# from outside (a probe, a debugging session that wants to attach). It
# is deliberately NOT propagated by `sandboxed_env`: a child process
# that imports this module is a different run and gets its own socket,
# so its teardown can never kill this run's server.
TMUX_SOCKET_ENV = "CENTRALE_ITEST_TMUX_SOCKET"
TMUX_SOCKET_PREFIX = "centrale-itest"


def new_tmux_socket_name():
    """A fresh socket name for one test-run process. Short on purpose:
    it lands inside a unix socket path, which is capped at ~108 bytes."""
    return f"{TMUX_SOCKET_PREFIX}-{os.getpid()}-{secrets.token_hex(3)}"


TMUX_SOCKET = os.environ.get(TMUX_SOCKET_ENV, "").strip() or new_tmux_socket_name()


def tmux_socket_path(socket_name=None):
    """The filesystem path tmux puts a `-L <name>` socket at: TMUX_TMPDIR
    if set and non-empty, else the literal /tmp (tmux's own fallback --
    it does not consult TMPDIR, which this tier redirects for the release
    tests), then `tmux-<uid>/<name>`.

    Needed because `kill-server` does NOT unlink the socket file: an
    empty socket is left behind for every name ever used, which is
    exactly the per-run litter a unique name would otherwise create (the
    machine this was written on had seven such strays). See
    `assert_test_tmux_footprint_gone`."""
    name = TMUX_SOCKET if socket_name is None else socket_name
    root = os.environ.get("TMUX_TMPDIR", "").strip() or "/tmp"
    return os.path.join(root, f"tmux-{os.getuid()}", name)


# Never hand out a port in the live browserPortBase range, or the live
# server's own port -- see the class docstring above.
FORBIDDEN_PORTS = set(range(6420, 6431)) | {7420}


def which(name):
    return shutil.which(name)


HAVE_GIT = which("git") is not None
HAVE_TMUX = which("tmux") is not None
HAVE_BACKLOG = which("backlog") is not None


# task-130: the release gate (scripts/release.sh) runs this tier against
# the staged snapshot and exports this variable around the run. With it
# set, a test that would SKIP for want of a tool FAILS instead, naming
# the tool -- the same switch tests/js_harness.py gives the frontend
# behavioural tier under CENTRALE_REQUIRE_NODE, for the same reason: a
# tier that skips itself whole still lets `unittest` report OK, and a
# release must not pass on a tier that never ran. Nothing outside a
# release sets it (tests/test_integration_base.py asserts that), so a
# development machine without tmux or backlog still gets a clean skip.
REQUIRE_INTEGRATION_ENV = "CENTRALE_REQUIRE_INTEGRATION"


def integration_required():
    """True when a caller has declared that this tier may not skip. Only
    a meaningful value arms it: unset, empty, whitespace and "0" do not."""
    return os.environ.get(REQUIRE_INTEGRATION_ENV, "").strip() not in ("", "0")


def _fail_instead_of_skip(reason):
    """A decorator that turns a class or test into a FAILURE carrying
    `reason` -- a failure rather than an error, because "this tier could
    not run" is a verdict the release gate must hear, not a crash, and
    rather than a skip, because a skip is an OK. On a class every
    `test*` method is replaced, so the count of what did not run is
    honest too, and so is `setUp` -- a fixture that shells out to the
    very tool that is missing would otherwise turn the verdict into an
    unrelated-looking error before the test method was reached. (On a
    single method the class's setUp still runs first, so a gate the
    fixture itself depends on belongs on the class, which is where every
    module in this tier puts it.)"""
    def fail(self):
        self.fail(reason)

    def decorate(target):
        if isinstance(target, type):
            for name in dir(target):
                if name.startswith("test") and callable(getattr(target, name)):
                    setattr(target, name, fail)
            target.setUp = fail
            target.setUpClass = classmethod(lambda cls: None)
            return target
        return fail

    return decorate


def require_tools(*names):
    """Class/method decorator: skips (with a clear reason) if any named
    tool is missing from PATH, rather than erroring out confusingly deep
    inside a real subprocess call -- unless a caller has set
    CENTRALE_REQUIRE_INTEGRATION (see above), in which case the same
    absence is a failure that names the missing tool(s)."""
    missing = [n for n in names if which(n) is None]
    reason = f"missing required tool(s) on PATH: {', '.join(missing)}"
    if missing and integration_required():
        return _fail_instead_of_skip(
            f"{reason} -- and {REQUIRE_INTEGRATION_ENV} is set, so this tier "
            "may not skip (a release gate runs it; see scripts/release.sh)")
    return unittest.skipUnless(not missing, reason)


def free_port():
    """Bind-then-release an ephemeral, OS-assigned port (bind to port 0),
    retrying if it happens to land in a reserved range. TOCTOU-prone by
    nature -- the same caveat server.port_is_free documents -- but good
    enough for picking a starting port for a short-lived test process."""
    for _ in range(200):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        if port not in FORBIDDEN_PORTS:
            return port
    raise RuntimeError("could not find a free ephemeral port outside the reserved ranges")


def port_listening(port, timeout=0.3):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


def wait_until_http_ok(url, timeout=10.0, interval=0.2):
    """Polls `url` until a GET succeeds with HTTP 200, tolerating
    connection-refused/timeout/not-ready-yet responses along the way --
    a listening TCP port (see port_listening) doesn't mean the app
    behind it is ready to actually answer HTTP requests yet, which was
    found to cause an intermittent flake when a single urlopen() call
    with a fixed timeout immediately followed a port_listening() check.
    Raises AssertionError (with the last error/status seen) on timeout,
    never a raw urllib exception."""
    import urllib.error
    import urllib.request

    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2.0) as resp:
                if resp.status == 200:
                    return
                last = f"HTTP {resp.status}"
        except Exception as exc:  # noqa: BLE001 - deliberately broad, see docstring
            last = repr(exc)
        time.sleep(interval)
    raise AssertionError(f"{url} never returned HTTP 200 within {timeout}s (last: {last})")


def wait_until(predicate, timeout=15.0, interval=0.1, message="condition not met in time"):
    """Polls predicate() until it's truthy or timeout elapses. Never a
    bare sleep-as-synchronization -- every wait in this test tier goes
    through this (or an equivalent bounded poll) so timing is generous
    but never unbounded."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(interval)
    if predicate():
        return
    raise AssertionError(message)


def run(cmd, cwd=None, env=None, timeout=30, check=True, input=None):
    """Thin subprocess.run wrapper for test setup/assertions (distinct
    from server.py's own _run boundary, which is what's under test) that
    raises a readable AssertionError, including captured output, on an
    unexpected non-zero exit."""
    proc = subprocess.run(
        cmd, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout, input=input
    )
    if check and proc.returncode != 0:
        raise AssertionError(
            f"command failed (exit {proc.returncode}): {' '.join(cmd)}\n"
            f"cwd={cwd}\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
        )
    return proc


# ---------------------------------------------------------------------------
# tmux sandboxing
# ---------------------------------------------------------------------------

def make_tmux_shim(shim_dir, socket_name=None):
    """Writes a `tmux` shim into shim_dir that forwards every invocation
    to the real tmux binary with `-L <this run's socket>` prepended.
    Centrale's own code (server.run_tmux) always shells out to a bare
    `tmux` with no -L flag and no way to override it without touching
    source, so prepending shim_dir to PATH is how a real
    server/spawn/harvest call gets transparently redirected to our
    dedicated test socket instead of the user's default one, without
    changing a line of Centrale code. The name is baked into the shim at
    write time, so a subprocess started with it lands on the same socket
    as this process's direct `tmux()` calls no matter what its own
    environment says. Returns the shim path."""
    real_tmux = which("tmux")
    if not real_tmux:
        raise RuntimeError("tmux not found on PATH")
    name = TMUX_SOCKET if socket_name is None else socket_name
    shim_path = os.path.join(shim_dir, "tmux")
    with open(shim_path, "w", encoding="utf-8") as f:
        f.write(f'#!/bin/sh\nexec "{real_tmux}" -L {name} "$@"\n')
    os.chmod(shim_path, 0o755)
    return shim_path


def sandboxed_env(shim_dir, cache_home=None, extra=None):
    """Base environment for a real server.py subprocess under test: a
    copy of the real environment with the tmux shim directory prepended
    to PATH, XDG_CACHE_HOME redirected to an isolated temp dir (see
    IntegrationCase.setUp's docstring for why this matters), plus any
    extra overrides (e.g. CENTRALE_SPAWN_CMD).

    The socket name is carried by the shim itself, not by a variable
    here: a child reaches this run's socket because the `tmux` it finds
    on PATH already has the `-L` baked in, and a child that instead
    imports this module is a separate run that must get a separate
    socket (see TMUX_SOCKET above)."""
    env = dict(os.environ)
    env["PATH"] = shim_dir + os.pathsep + env.get("PATH", "")
    if cache_home:
        env["XDG_CACHE_HOME"] = cache_home
    if extra:
        env.update(extra)
    return env


def tmux(*args, socket_name=None, check=False, timeout=15):
    """Runs `tmux -L <this run's socket> <args>` directly against the real
    tmux binary, for test-driver-side session inspection/cleanup. Explicit
    -L here, rather than going through the PATH shim, since there's
    nothing ambiguous to redirect -- this *is* the dedicated-socket
    invocation. `socket_name` addresses some OTHER run's socket, which
    only the isolation tests (test_tmux_socket_integration.py) have any
    business doing."""
    real_tmux = which("tmux")
    name = TMUX_SOCKET if socket_name is None else socket_name
    return run([real_tmux, "-L", name, *args], check=check, timeout=timeout)


def attached_client_sizes(socket_name=None):
    """`WIDTHxHEIGHT` for every client attached to the dedicated test
    socket. Empty when none are -- which is the state that made
    task-113's geometry look correct and task-152's bug invisible, so a
    test that needs a client attached should assert on this rather than
    assume."""
    proc = tmux("list-clients", "-F", "#{client_width}x#{client_height}",
                socket_name=socket_name, check=False)
    if proc.returncode != 0:
        return []
    return [line.strip() for line in (proc.stdout or "").splitlines() if line.strip()]


def attach_pty_client(session_name, columns=142, rows=30, timeout=10.0, socket_name=None):
    """Attach a REAL tmux client to `session_name` on the dedicated test
    socket, over a pty sized exactly `columns`x`rows` (task-152).

    This exists because a tmux server with no client attached behaves
    differently from every real one: `window-size` defaults to `latest`,
    which is resolved against the most recently attached client when a
    session is BORN, so `new-session -x/-y` only survives on a socket
    nobody is looking at. The owner's terminal is always attached to the
    server their agents spawn on, so a test that wants to exercise the
    real condition has to put a client there itself.

    A pty rather than `script(1)`: the size is set explicitly with
    TIOCSWINSZ instead of inherited from whatever the test runner's
    terminal happens to be (there may not be one under CI at all), and
    it adds no tool to require_tools. A daemon thread drains the master
    end, because an undrained pty fills and blocks tmux's redraw.

    Returns the Popen. The caller is responsible for cleanup; the
    IntegrationCase helper below (attach_client) does it via addCleanup.
    """
    # Local imports: the default suite imports this module for one pure
    # text helper (see this package's README), and pty/termios are only
    # ever needed by the handful of tests that want a real client.
    import fcntl
    import pty
    import struct
    import termios
    import threading

    real_tmux = which("tmux")
    if not real_tmux:
        raise RuntimeError("tmux not found on PATH")
    name = TMUX_SOCKET if socket_name is None else socket_name
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", rows, columns, 0, 0))
    proc = subprocess.Popen(
        [real_tmux, "-L", name, "attach", "-t", session_name],
        stdin=slave, stdout=slave, stderr=slave, start_new_session=True,
    )
    os.close(slave)

    def drain():
        try:
            while os.read(master, 65536):
                pass
        except OSError:
            pass
        finally:
            # tmux is gone (or the pty broke): nothing else holds this
            # end, and a per-test fd leak is still a leak.
            try:
                os.close(master)
            except OSError:
                pass

    threading.Thread(target=drain, daemon=True).start()
    want = f"{columns}x{rows}"
    wait_until(
        lambda: want in attached_client_sizes(socket_name=name), timeout=timeout,
        message=f"no {want} tmux client attached to the {name} socket in time")
    return proc


def pane_pid(session_name, socket_name=None):
    """The pid of a tmux session's (first window's) active pane, on the
    dedicated test socket. None if the session doesn't exist."""
    proc = tmux("list-panes", "-t", session_name, "-F", "#{pane_pid}",
                socket_name=socket_name, check=False)
    if proc.returncode != 0:
        return None
    lines = (proc.stdout or "").strip().splitlines()
    return int(lines[0]) if lines else None


def read_proc_cmdline(pid):
    """The real argv of a live process, read from /proc -- used to prove
    which command a tmux pane is actually running (e.g. that a resumed
    session launched the *configured* resumeCmd, not some other
    fallback), since a shell's exec-tail-call means the pane's own
    command can differ from what was literally passed to `tmux
    new-session`. Returns [] if the pid is gone or unreadable."""
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            raw = f.read()
    except OSError:
        return []
    return [part.decode("utf-8", "replace") for part in raw.split(b"\x00") if part]


def kill_test_tmux_session(name, socket_name=None):
    tmux("kill-session", "-t", name, socket_name=socket_name, check=False)


def kill_test_tmux_server(socket_name=None):
    """Idempotent: tears down the whole dedicated test tmux server.
    Tolerates 'no server running' (nothing to kill)."""
    if not HAVE_TMUX:
        return
    tmux("kill-server", socket_name=socket_name, check=False)


def assert_test_tmux_footprint_gone(socket_name=None):
    """Kills the test tmux server (idempotent) and asserts this run left
    NOTHING behind on its socket axis: no server, therefore no session
    running under it, and no socket file either -- used as the final
    guarantee at the end of every module in this package (see each
    module's tearDownModule).

    The socket file is the half that only matters once the name is
    unique per run (task-161): `kill-server` shuts the server down but
    does not unlink `<tmpdir>/tmux-<uid>/<name>`, so a per-run name would
    quietly litter one empty socket per run where the old fixed name
    litters exactly one forever. Asserting the removal, rather than
    trusting it, is what makes "namespace every axis, footprint zero"
    (AGENTS.md) a property the tier keeps rather than a convention it
    remembers."""
    if not HAVE_TMUX:
        return
    kill_test_tmux_server(socket_name=socket_name)
    proc = tmux("list-sessions", socket_name=socket_name, check=False)
    combined = f"{proc.stdout}{proc.stderr}".lower()
    if proc.returncode == 0:
        raise AssertionError(f"test tmux server still has sessions: {proc.stdout}")
    # tmux reports a torn-down/never-started server via a non-zero exit
    # and one of a few "no server" phrasings on stderr -- the same
    # tolerance server.list_sessions() itself applies.
    known_gone_phrases = ("no server running", "no such file or directory", "error connecting")
    if not any(p in combined for p in known_gone_phrases):
        raise AssertionError(f"unexpected tmux list-sessions output while asserting shutdown: {combined}")
    path = tmux_socket_path(socket_name)
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise AssertionError(f"could not remove the test tmux socket file {path}: {exc}")
    if os.path.exists(path):
        raise AssertionError(f"test tmux socket file still present after teardown: {path}")


# ---------------------------------------------------------------------------
# process-tree cleanup
# ---------------------------------------------------------------------------

def kill_process_tree(proc, sig=signal.SIGTERM):
    """Best-effort: signals the whole process group `proc` belongs to,
    not just proc.pid -- UNLESS that group is this test process's own
    (i.e. `proc` was launched without start_new_session=True and so
    inherited our group), in which case killpg would signal this test
    runner too; only proc.pid is signaled in that case.

    The whole-group behavior matters for `backlog browser`: it was
    empirically observed (see tests_integration/README.md) to fork a
    real server process that gets reparented away from the launched
    wrapper almost immediately (to the user's systemd --user instance)
    while remaining in the *same* process group as the wrapper.
    Signaling only proc.pid (what browser.py's own atexit cleanup does
    today) leaves that real server running and the port still bound --
    an orphan. Signaling the whole process group reaches it too, as long
    as that group isn't shared with us. Falls back to signaling just
    proc.pid if the group lookup fails (e.g. already reaped)."""
    try:
        pgid = os.getpgid(proc.pid)
        if pgid == os.getpgid(0):
            proc.send_signal(sig)
        else:
            os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError, OSError):
        with contextlib.suppress(Exception):
            proc.send_signal(sig)


def pid_alive(pid):
    """True if a signal (0, a no-op probe) can be delivered to pid --
    i.e. it exists and this process may signal it. Never raises."""
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def find_pids_listening_on(port):
    """Real PIDs with a LISTEN socket on 127.0.0.1:<port>, via `lsof`.
    Returns [] if lsof is missing or nothing is listening -- never
    raises. This exists because process-group-based cleanup
    (kill_process_tree) is time-sensitive: a `backlog browser` child was
    observed to still share its wrapper's pgid immediately after launch,
    but to have since detached (setsid) into its own group by the time a
    longer-running test's cleanup runs, no longer reachable via killpg
    at all (see docs/board.md). Killing whatever is *actually bound to the
    port we handed out* is the reliable fallback regardless of process
    tree shape."""
    lsof = which("lsof")
    if not lsof:
        return []
    proc = subprocess.run(
        [lsof, "-t", f"-i:{port}", "-sTCP:LISTEN"], capture_output=True, text=True
    )
    pids = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if line.isdigit():
            pids.append(int(line))
    return pids


def kill_port(port, sig=signal.SIGKILL):
    """Best-effort: kills every real process listening on 127.0.0.1:port.
    Never raises. See find_pids_listening_on for why this, rather than
    process-group signaling alone, is the reliable cleanup for a
    `backlog browser` child."""
    for pid in find_pids_listening_on(port):
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.kill(pid, sig)


def terminate_and_wait(proc, timeout=5.0):
    """Terminates proc's whole process group (see kill_process_tree),
    escalating to SIGKILL if it's still around after `timeout`. Never
    raises."""
    if proc is None:
        return
    with contextlib.suppress(Exception):
        if proc.poll() is not None:
            return
    kill_process_tree(proc, signal.SIGTERM)
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        kill_process_tree(proc, signal.SIGKILL)
        with contextlib.suppress(Exception):
            proc.wait(timeout=timeout)


# ---------------------------------------------------------------------------
# Temp git + backlog repo fixtures
# ---------------------------------------------------------------------------

def init_backlog_repo(base_dir, name="repo", extra_files=None):
    """Creates a fresh git repo at base_dir/name with an initial commit,
    then runs the real `backlog init --defaults` CLI in it and commits
    the result. Returns the repo path. Always a tmp dir the caller owns
    -- never a real project."""
    path = os.path.join(base_dir, name)
    os.makedirs(path, exist_ok=True)
    run(["git", "init", "-q", "-b", "main", "."], cwd=path)
    run(["git", "config", "user.email", "itest@example.com"], cwd=path)
    run(["git", "config", "user.name", "Centrale Integration Tests"], cwd=path)
    readme = os.path.join(path, "README.md")
    with open(readme, "w", encoding="utf-8") as f:
        f.write(f"# {name}\n\nIntegration test fixture repo. Safe to delete.\n")
    for rel, content in (extra_files or {}).items():
        full = os.path.join(path, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as f:
            f.write(content)
    run(["git", "add", "-A"], cwd=path)
    run(["git", "commit", "-q", "-m", "initial commit"], cwd=path)
    run(["backlog", "init", "--defaults", name], cwd=path, timeout=60)
    run(["git", "add", "-A"], cwd=path)
    run(["git", "commit", "-q", "-m", "backlog init"], cwd=path)
    return path


# `backlog task create --plain` prints the new task's file path first,
# then a full plain-text view of the task:
#
#     File: /abs/path/backlog/tasks/task-1 - Probe title.md
#
#     Task TASK-1 - Probe title
#     ==========================
#     ...
#     Parent: TASK-7
#
# Both patterns below are deliberately anchored. An unanchored search for
# `task-(\d+)` anywhere in that output picks up whatever came first --
# including a `task-<n>` segment of the absolute path in the `File:` line,
# so a scratch repo under e.g. `.../centrale-task-81/.../scratchpad/`
# reported TASK-81 for what was really TASK-1 (task-88). The failure was
# silent: later steps just complained the task didn't exist.
_TASK_ID_FROM_HEADING_RE = re.compile(r"^Task\s+TASK-(\d+(?:\.\d+)*)\b", re.MULTILINE)
# Fallback, scoped to the `File:` line only, matching the `tasks/task-N`
# *filename* component -- the last one on the line, so a directory called
# `tasks/task-81` earlier in the path can't win over the real filename.
_FILE_LINE_RE = re.compile(r"^File:.*$", re.MULTILINE)
_TASK_ID_FROM_FILENAME_RE = re.compile(r"/tasks/task-(\d+(?:\.\d+)*)\b")


def parse_created_task_id(stdout):
    """The canonical ID (e.g. "TASK-1", "TASK-1.2") of the task a
    `backlog task create --plain` run just created, from its captured
    stdout. Raises AssertionError if no id can be found.

    Split out of create_task() so the parsing is unit-testable against
    captured CLI output, without a real `backlog` CLI (see
    tests/test_integration_base.py)."""
    m = _TASK_ID_FROM_HEADING_RE.search(stdout or "")
    if m:
        return f"TASK-{m.group(1)}"
    file_line = _FILE_LINE_RE.search(stdout or "")
    if file_line:
        matches = _TASK_ID_FROM_FILENAME_RE.findall(file_line.group(0))
        if matches:
            return f"TASK-{matches[-1]}"
    raise AssertionError(f"could not parse task id from `backlog task create` output:\n{stdout}")


def create_task(repo_path, title, description=None, assignee=None, status=None,
                 acceptance_criteria=None, timeout=30):
    """Creates a task via the real `backlog task create --plain` CLI and
    returns its canonical ID (e.g. "TASK-1"), parsed from the
    `Task TASK-N - <title>` line the CLI prints (see
    parse_created_task_id) -- never from the absolute path on the
    preceding `File:` line, which can itself contain a `task-<n>`
    segment."""
    args = ["backlog", "task", "create", title, "--plain"]
    if description is not None:
        args += ["-d", description]
    if assignee is not None:
        args += ["-a", assignee]
    if status is not None:
        args += ["-s", status]
    for ac in acceptance_criteria or ():
        args += ["--ac", ac]
    proc = run(args, cwd=repo_path, timeout=timeout)
    return parse_created_task_id(proc.stdout)


def probe_spawn_cmd(sleep_seconds=300, marker="itest-probe"):
    """A harmless CENTRALE_SPAWN_CMD value: `sh -c "sleep N" marker`. Note
    the `sh -c` wrapping -- a bare `sleep 300` would receive the prompt
    Centrale always appends as a second, non-numeric argument and exit
    immediately with 'invalid time interval', taking the tmux session
    down with it (see the CENTRALE_SPAWN_CMD section of docs/agents.md)."""
    return f'sh -c "sleep {sleep_seconds}" {marker}'


# ---------------------------------------------------------------------------
# Deploying an isolated copy of the app (for real server.py subprocess tests)
# ---------------------------------------------------------------------------

# version.py is server.py's very first import (task-107's single source of
# truth for the version number). Leaving it out here does not degrade a
# deployed snapshot -- it stops it dead with ModuleNotFoundError before
# any test can reach it, which is what happened to the whole real-server
# half of this tier between task-107 landing and task-119 finding it.
APP_MODULES = ("server.py", "spawn.py", "browser.py", "harvest.py", "settings.py",
               "centrale_notify.py", "version.py")


def deploy_app(dest_dir):
    """Copies Centrale's server modules + static assets into dest_dir,
    without touching the real source tree. server.py resolves its own
    config path from its *script* directory
    (`os.path.dirname(os.path.abspath(__file__))`), not the process cwd,
    so this is the only way to point a real, unmodified server.py
    process at a sandboxed projects.json: deploy a copy next to that
    config, rather than the real one next to the real projects.json.
    Returns dest_dir."""
    os.makedirs(dest_dir, exist_ok=True)
    for name in APP_MODULES:
        shutil.copy2(os.path.join(CENTRALE_ROOT, name), os.path.join(dest_dir, name))
    shutil.copytree(os.path.join(CENTRALE_ROOT, "static"), os.path.join(dest_dir, "static"))
    return dest_dir


def write_projects_json(app_dir, config_dict):
    path = os.path.join(app_dir, "projects.json")
    import json
    with open(path, "w", encoding="utf-8") as f:
        json.dump(config_dict, f, indent=2)
    return path


# ---------------------------------------------------------------------------
# Base test case
# ---------------------------------------------------------------------------

class IntegrationCase(unittest.TestCase):
    """Base class for every integration test: a private tmp dir and a
    tmux PATH shim per test, both cleaned up via addCleanup (which runs
    even when the test fails or errors).

    Also isolates XDG_CACHE_HOME to a per-test temp dir: browser.py's
    orphan-process registry (added in task-28, see
    browser.registry_path()) defaults to ~/.cache/centrale/browsers.json,
    and a real server.py subprocess under test both reads AND WRITES
    that file at every browser launch/sweep. Without this override, an
    early version of this test tier's server-lifecycle test did exactly
    that against the real ~/.cache/centrale/browsers.json before this was
    caught -- see tests_integration/README.md's sandboxing note. Every
    test in this class gets an isolated one by default, whether or not
    it happens to touch browser.py."""

    def setUp(self):
        super().setUp()
        self.tmp_dir = tempfile.mkdtemp(prefix="centrale-itest-")
        self.addCleanup(shutil.rmtree, self.tmp_dir, ignore_errors=True)

        self.shim_dir = tempfile.mkdtemp(prefix="centrale-itest-shim-")
        self.addCleanup(shutil.rmtree, self.shim_dir, ignore_errors=True)
        if HAVE_TMUX:
            make_tmux_shim(self.shim_dir)

        self.cache_home = tempfile.mkdtemp(prefix="centrale-itest-cache-")
        self.addCleanup(shutil.rmtree, self.cache_home, ignore_errors=True)

        # Redirect *this test process's own* PATH/XDG_CACHE_HOME so any
        # in-process call into server.run_tmux / spawn.py / browser.py
        # (which shell out to a bare `tmux`, or read/write the real
        # per-user cache dir) is transparently sandboxed too, not just
        # subprocess tests that pass an explicit env=. Restored on
        # cleanup.
        patched_path = self.shim_dir + os.pathsep + os.environ.get("PATH", "")
        patcher = mock.patch.dict(os.environ, {"PATH": patched_path, "XDG_CACHE_HOME": self.cache_home})
        patcher.start()
        self.addCleanup(patcher.stop)

        self._tracked_procs = []
        self.addCleanup(self._cleanup_tracked_procs)
        self._tracked_sessions = []
        self.addCleanup(self._cleanup_tracked_sessions)
        self._tracked_ports = []
        self.addCleanup(self._cleanup_tracked_ports)

    def track_proc(self, proc):
        """Registers a real subprocess.Popen for guaranteed cleanup
        (whole process group, escalating to SIGKILL) even if the test
        fails before reaching its own cleanup code."""
        self._tracked_procs.append(proc)
        return proc

    def track_session(self, name):
        """Registers a tmux session name for guaranteed kill-session
        cleanup, independent of end-of-module server teardown."""
        self._tracked_sessions.append(name)
        return name

    def attach_client(self, session_name, columns=142, rows=30):
        """A real tmux client, on a pty of the given size, attached to
        the dedicated test socket for the rest of this test (task-152 --
        see attach_pty_client for why a test would want one). Torn down
        via addCleanup, so a failing test still leaves the socket
        clientless."""
        proc = attach_pty_client(session_name, columns=columns, rows=rows)
        self.addCleanup(terminate_and_wait, proc)
        return proc

    def track_port(self, port):
        """Registers a port for guaranteed cleanup by killing whatever's
        actually listening on it (see kill_port) -- the reliable
        fallback for a real `backlog browser` child, whose process-group
        membership can't be relied on once it's been alive a while (see
        find_pids_listening_on)."""
        self._tracked_ports.append(port)
        return port

    def _cleanup_tracked_procs(self):
        for proc in self._tracked_procs:
            terminate_and_wait(proc)

    def _cleanup_tracked_sessions(self):
        for name in self._tracked_sessions:
            kill_test_tmux_session(name)

    def _cleanup_tracked_ports(self):
        for port in self._tracked_ports:
            kill_port(port)

    def env(self, extra=None):
        return sandboxed_env(self.shim_dir, cache_home=self.cache_home, extra=extra)

    def wait_for_http(self, url, timeout=10.0):
        import urllib.error
        import urllib.request

        def _ok():
            try:
                with urllib.request.urlopen(url, timeout=1.0):
                    return True
            except urllib.error.HTTPError:
                return True  # server answered, even with a non-2xx status
            except Exception:
                return False

        wait_until(_ok, timeout=timeout, message=f"server never answered at {url}")
