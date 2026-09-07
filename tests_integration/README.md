# Integration test tier

Centrale has two test tiers. `tests/` — the default suite — mocks every
subprocess boundary (`run_backlog`, `run_git`, `run_tmux`,
`launch_browser_process`, ...) so it is fast and hermetic. This directory is
the second tier: it exercises **real** `git`, `tmux` and `backlog` CLI
processes — and, for three modules, a real `server.py` process — in fully
sandboxed environments.

The two tiers answer different questions. The mocked suite checks that the
code does the right thing with the answers it gets; this tier checks that
the real tools give those answers. The bugs it exists for are the ones a
mock cannot express by construction: orphaned `backlog browser` processes,
port squatting, stale tmux sessions, and git behaviour that turns out not to
match what the code assumed.

## Running it

```bash
python3 -m unittest discover tests_integration
```

This tier is **never** collected by `python3 -m unittest discover tests`.
That is deliberate: the default suite stays fast and free of real
processes. The one thing the default suite imports from here is `base.py`'s
pure text-parsing helper `parse_created_task_id`, covered by
`tests/test_integration_base.py` — importing `tests_integration.base` runs
no subprocesses and touches nothing outside the process (a few
`shutil.which` lookups), and that parser is worth a cheap regression test
because it once silently returned the wrong task id for a whole integration
run. No `test_*_integration.py` module is ever imported by the default
suite. The one import in the other direction is
`test_settings_integration.py` reaching for `tests/js_harness.py`, the
shared DOM shim the frontend is run over: it is how this repo executes
its JavaScript anywhere, and a second copy of it here would be the
duplication task-108 removed. It runs no test of its own on import.

Run a single module the same way as any other unittest package:

```bash
python3 -m unittest tests_integration.test_spawn_integration -v
```

Requires `git`, `tmux` and the `backlog` CLI on `PATH` — plus `tar` and
`node`, which only the release-script module needs (a fixture release exits
3 without them, exactly as a real one does). A module whose tests need a
tool that is missing skips cleanly with a clear reason
(`@base.require_tools(...)`) rather than erroring; nothing here should ever
hard-fail just because an optional dependency isn't installed on the machine
running it.

The one exception is a release. `scripts/release.sh` runs this tier against
the staged snapshot as part of its gate (task-130) and exports
`CENTRALE_REQUIRE_INTEGRATION=1` around the run; with that set,
`require_tools` turns the same missing tool into a **failure** that names
it, because a tier that skips itself whole still lets `unittest` report OK
and a release must not pass on a tier that never ran. Nothing outside a
release sets the variable (`tests/test_integration_base.py` asserts that),
and no test here skips for any other reason — a test that cannot run in
the gate must fail with a stated reason, never skip. The sandbox
guarantees below are what make running the tier from a staging directory
on a maintainer's machine safe; every path in it is resolved from
`base.py`'s own location, never from the process cwd or from a checkout's
git history, so the staged copy tests the staged code.

## Sandbox guarantees

Every test here is built on `tests_integration/base.py`, which enforces:

- **Repos**: every git/backlog repo is a fresh temp directory created with
  `git init` plus the real `backlog init --defaults` CLI
  (`base.init_backlog_repo`). Never your own repos.
- **tmux**: every real tmux session — whether started directly by a test or
  indirectly by a real `spawn.spawn()`/`spawn.resume()` call — runs on a
  dedicated socket **named uniquely per test-run process**
  (`centrale-itest-<pid>-<random>`, `base.TMUX_SOCKET`), never your default
  tmux server and never a socket another run is on. Centrale's own code
  always shells out to a bare `tmux` with no way to pass `-L` from the
  outside, so this is done by prepending a `tmux` shim to `PATH`
  (`base.make_tmux_shim`) that forwards every invocation to
  `tmux -L <this run's socket> ...` — transparent to the code under test,
  no source changes required, and baked into the shim at write time so a
  subprocess lands on the same socket as the driver's own `-L` calls.
  Set `CENTRALE_ITEST_TMUX_SOCKET` to pin the name (to attach to a run and
  watch it, say); it is deliberately not propagated to child processes, so
  a child that imports `base` is a separate run with a separate socket and
  its teardown can never kill yours. The name was a fixed `centrale-itest`
  until task-161, which meant your run, an agent's, and the copy
  `scripts/release.sh` runs against the staged snapshot all shared one tmux
  server, where any module's teardown killed the others' sessions.
  Every module's `tearDownModule` calls
  `base.assert_test_tmux_footprint_gone()`, which kills this run's server,
  asserts it is actually gone, and removes the socket file and asserts
  *that* is gone too — `kill-server` does not unlink it, so a per-run name
  would otherwise litter one dead socket per run in `/tmp/tmux-<uid>/`. It
  is idempotent and every module does it, so the tier's footprint on this
  axis is guaranteed zero by the time a full `discover` run finishes,
  whichever modules actually ran.
  `test_tmux_socket_integration.py` is the coverage for all of that,
  including two independently namespaced harnesses proven not to see or
  kill each other.
- **tmux *clients***: one group of tests needs the socket to have a client
  attached, because a tmux server nobody is looking at resolves
  `window-size latest` differently from every real one (task-152 —
  `base.attach_pty_client` / `IntegrationCase.attach_client`). That client
  is a real `tmux attach` on a `pty.openpty()` pair sized by `TIOCSWINSZ`,
  not the test runner's own terminal (there may not be one), so it adds no
  tool to `require_tools` and inherits no size from the environment. It
  attaches only to this run's own socket, is registered for
  `addCleanup` teardown like any other process, and a daemon thread drains
  the master end so an undrained pty cannot block tmux's redraw.
- **Remotes**: the release-script tests push for real, but only ever into a
  `git init --bare` directory inside the test's own temp dir — nothing in
  this tier makes network contact of any kind. `TMPDIR` is redirected into
  that temp dir too, so `release.sh`'s staging directories and archive
  tarballs land there rather than in the machine's `/tmp`; one staging tree
  is deliberately left behind on a gate failure, and is cleaned up with the
  rest of the test's tree.
- **Ports**: every port is chosen by binding to port 0 and releasing it
  (`base.free_port()`), with the reserved `browserPortBase` range
  (6420–6430) and Centrale's own default port (7420) explicitly excluded.
  TOCTOU-prone by nature — the same caveat `server.port_is_free` documents —
  but fine for a short-lived test process.
- **"Agents"**: nothing here ever launches a real `claude` or `codex`. Real
  spawn/resume flows use the spawn-command override with a harmless probe
  (`base.probe_spawn_cmd`, `sh -c "sleep N" marker`), or a configured agent
  whose `cmd`/`resumeCmd` is a bare `sleep`.
- **Your live server and cache**: nothing here binds port 7420 or touches
  the checked-out repo's own `projects.json`. A real `server.py` process
  under test is a script-level *copy* deployed to an isolated temp directory
  (`base.deploy_app`) — `server.py` resolves its config path relative to its
  script directory rather than the process cwd, so this is the only way to
  point a real, unmodified `server.py` at a sandboxed `projects.json`
  without editing source. `XDG_CACHE_HOME` is redirected to a per-test temp
  dir (`IntegrationCase.cache_home`) for every `IntegrationCase` subclass,
  whether or not the test touches `browser.py`, so the browser-launcher's
  orphan-process registry (`browser.registry_path()`, which otherwise
  defaults to `~/.cache/centrale/browsers.json`) is never the real one.
- **Cleanup**: every test's setup, and any process, session or port it
  touches, is registered via `addCleanup`
  (`IntegrationCase.track_proc` / `track_session` / `track_port`), so it
  runs even when the test fails or raises. `track_port` is the one that
  matters most: killing a process *tree* is unreliable here, because a
  `backlog browser` child shares its parent's process group immediately
  after launch but has detached into its own by the time a longer test's
  cleanup runs. Killing whatever is actually listening on a port we handed
  out (`base.kill_port`, via `lsof`) works regardless of process-tree shape,
  so that is what `test_browser_integration.py` and
  `test_server_lifecycle_integration.py` rely on.

## What's covered, and where

| Area | Module | Notes |
|---|---|---|
| Browser launcher | `test_browser_integration.py` | Real launch and serving, reuse while alive, killed-child relaunch, a port pre-occupied by a foreign process. |
| Server process lifecycle | `test_server_lifecycle_integration.py` | A real `server.py` subprocess, SIGTERM, and the registry/boot-sweep recovery path — including the fact that `backlog browser` forks a real listener that detaches from the pid `Popen` returns, so cleanup has to resolve and kill both. |
| Spawn (end to end) | `test_spawn_integration.py` | Real worktree plus a real tmux session running an override command, the pre-worktree claim commit, the duplicate-session 409, kill-session-then-respawn reuse. Plus session geometry **with a real client attached to the socket** (task-152): the condition a clean-socket test cannot express, under which `new-session -x/-y` is overridden at birth and the `window-size manual` pin freezes the wrong size — including the proof that a full-height alternate-screen pane really does hand `capture-pane` every line the theater asks for. |
| Harvest (end to end) | `test_harvest_integration.py` | A genuinely finished task branch merging through all five real gates, a conflicting branch failing the real merge-clean gate, and the main-checkout dirt rule: unrelated dirt merges fine, overlapping unstaged/untracked dirt refuses with the file list, and a staged-but-unrelated change refuses unconditionally (real, empirically verified git behaviour, not a choice Centrale made). |
| Resume | `test_resume_integration.py` | A real dirty, session-less worktree resumed via a configured `resumeCmd`, and the fallback-to-fresh-start path. |
| Resume to reconcile | `test_reconcile_integration.py` | A *semantic* collision git sees no conflict in: a branch green on its own that fails a check the base branch has since landed. The merge gate must fail on the scratch-merged tree and report how far behind the branch is; an up-to-date branch failing the same gate must not carry that hint; and a reconcile resume must start the agent in the existing worktree with the reconcile prompt while Centrale merges nothing itself. |
| Branch cleanup | `test_cleanup_integration.py` | `POST /api/cleanup-branch` against a real sandboxed `server.py`: a branch merged out-of-band and still checked out in a worktree Centrale does not manage gets an honest 409 and keeps both branch and worktree, while parked branches and Centrale's own worktrees still clean up. |
| Discarding an attempt | `test_discard_integration.py` | `POST /api/discard-attempt` and `POST /api/abandon-worktree` against a real sandboxed `server.py`. Two claims here need a real git object store: that the recovery tag keeps the discarded commits alive through `git reflog expire --expire-unreachable=now --all && git gc --prune=now` (without it nothing references them once the worktree and branch are both gone) so the recovery command really works, and that `spawn._ensure_worktree` afterwards cuts a brand-new branch at the base rather than reusing the discarded one. Plus: the preview's counts matching what the discard then reports, an abandon leaving a parked branch the board still sees, the task status untouched, and both routes refusing an external checkout with nothing damaged. |
| Release script | `test_release_integration.py` | The real `scripts/release.sh` driven against local bare "scratch remotes": the foreign-identity refusal that stops a GitHub auto-init README from becoming the root of the public history, a `--dry-run` written after the message still counting as a flag, environment-over-`.release-remote` precedence, a gate failure that pushes nothing and keeps the staged snapshot, the secret/identity scan refusing a seeded credential or home path before the suite runs and a missing scan failing rather than skipping, and a missing tool on the release machine reported as such (exit 3) rather than as a bad snapshot. Plus the versioning half (task-107): the annotated `v<version>` tag cut from the snapshot's own constant and pushed with the release commit under the release identity (never the maintainer's), a changed snapshot under an already-published version refused while unchanged content stays the benign "nothing to release", a bump with no CHANGELOG section failing the gate, and a snapshot with no `version.py` refused outright. |
| The first-run settings path | `test_settings_integration.py` | Zero config through add, configure and remove, clicked through the REAL `static/*.js` under node against a real server, a real git repo with no Backlog.md in it (so the add runs the real `backlog init`) and a real `projects.json` read off disk at each step. Task-167's bug -- Remove disabled for the only configured project, so a first project added with a typo could not be undone -- plus the second one that walk exposed, an add rendering its own new row while its in-flight flag was still set. The one module here whose server runs in-process (there is no `--port` flag, and true zero config means no `projects.json` to put one in, so a subprocess would bind the developer's own 7420) and the one that imports `tests/js_harness.py` (a second copy of the DOM shim here is exactly the duplication task-108 removed). |
| The tier's own tmux sandbox | `test_tmux_socket_integration.py` | Task-161's namespacing, proven rather than assumed: two independently namespaced harnesses that neither list nor kill each other's sessions, the PATH shim / direct `-L` calls / an attached pty client all landing on one socket, a fresh socket name per test-run *process* (checked in real child processes, not in-process) with `CENTRALE_ITEST_TMUX_SOCKET` able to pin it, and a teardown that leaves neither a server nor a socket file behind — including for a run that never started one. |

`test_spawn_integration.py`, `test_harvest_integration.py`,
`test_resume_integration.py` and `test_reconcile_integration.py` call
`spawn.py`/`harvest.py` directly, in-process, rather than through a live
HTTP server: their subprocess calls are just as real, and standing up a
server would add nothing. `test_settings_integration.py` does serve the
real routes over real HTTP, but from a `CentraleHTTPServer` in the test
process on an ephemeral port rather than a `server.py` subprocess: a
subprocess takes its port from the `projects.json` beside it and there is
no `--port` flag (`build_arg_parser` says why), so a test that starts
from *no* `projects.json` at all — which is the state it exists to walk —
could only be a subprocess bound to the default 7420, the port the
developer's own Centrale is on. `test_server_lifecycle_integration.py` and
`test_cleanup_integration.py`/`test_discard_integration.py` are the ones
that genuinely need a real `server.py` process — the first for its signal
handling, the others for the HTTP routes under test. `test_release_integration.py` needs neither: it drives
`scripts/release.sh` as a subprocess, the way a release actually runs it.
