# Centrale architecture

Centrale is a local, stdlib-only web dashboard that aggregates Backlog.md task
boards from multiple repos into one kanban view and can spawn a coding agent
(Claude Code by default) on a task inside a git worktree. It stores no data of
its own: every repo's `backlog/` folder stays the source of truth, read through
the `backlog` CLI (`--json` everywhere it exists — see the milestone-title
exception in [docs/api.md](api.md#get-apiboard)'s field list), never by parsing
task markdown directly. The server binds `127.0.0.1` only; the one exception is
the opt-in, read-only status page on its own listener (`status_page.py`,
decision-6).
[docs/api.md](api.md) is the authority on the HTTP contract — every route, a
real response for each, and every error status; this chapter covers the module
layout behind it.

## Module layout

| Path | Role |
|---|---|
| `system_status.py` | Read-only hub and worker resource readers, tolerant JSON parser and shared 30-second in-memory cache behind `/api/system`; worker probes use the existing bounded machine boundary in background threads (task-242) |
| `server.py` | Backend: config loading, board aggregation, subprocess boundaries, HTTP server + routing |
| `spawn.py` | Worktree + tmux spawn engine behind `POST /api/spawn` |
| `fleet.py` | Bounded durable fleet observation journal (task-186), loaded explicitly at startup; never a source of live badges or task status |
| `orchestrator.py` | Optional condition-backed notification history for `GET /api/orchestrator-wait`, fed by existing hook transitions and harvest attempt results (task-174). No task decisions or subprocesses |
| `harvest.py` | Gated merge engine behind `POST /api/harvest` (and the live `GET /api/harvest-progress` record) |
| `settings.py` | Settings read/write behind `/api/settings` — a whitelisted subset of `projects.json`, including (task-78) the whole `agents` map for the modal's Agents editor. Also the opt-in Backlog/pointer setup shared by add-project and `/api/setup-project` (task-175), with commits scoped to changed files |
| `browser.py` | Per-project `backlog browser` launcher behind `POST /api/browser` |
| `status_page.py` | The opt-in, read-only status page for a phone (task-212, decision-6): when `statusPage.enabled`, a SECOND `ThreadingHTTPServer` with its own `StatusHandler` (not a `Handler` subclass) on `statusPage.port`, every interface unless `statusPage.bind` narrows it. Exact-path allowlist (the page, `/api/status`, five static files), 404 for anything else or without the key, 405 for any method but GET/HEAD. The key is generated into the state directory on first use. `/api/status` is `strip_snapshot()`'s allowlist over `server.get_fleet(..., arm_input=False)`. `apply()` starts, stops or moves the listener (main() at startup, `POST /api/settings` on save) and never raises; `doctor_check()` is `--check`'s part. Address discovery for the links goes through the patchable `run_ip_addr` boundary |
| `machines.py` | Remote machines (doc-3, task-206): `run_batch()` -- with `run_on_machine()` its one-command form -- is the one way to run a command on a machine: locally for the hub (`server._run`), otherwise ONE `ssh` round trip per batch through a generated ssh config (user's `~/.ssh/config` first, then `BatchMode`, `ConnectTimeout`, `ServerAlive*`, a kept-open master with a short `ControlPath` under `CENTRALE_SSH_DIR`), running `timeout -k <budget> sh -c <script>` built with `shlex` quoting, where a worker path's `~` is left for the worker's shell (`MachinePath`). Each result is `unreached`, `answered` or `unknown`, told apart by nonce markers the script prints. `git_ssh_env()` gives git's own ssh the same connection. `refresh()` is a board or fleet refresh's one batch per machine, in parallel, under a per-machine budget below `refreshIntervalSeconds`. Also where a task is (task-224, doc-3 revision 5): `locate()` derives a `Location` from the hub's worktree list and branch plus, for each machine the project lists, its project repository's worktree list and task branches in one round trip apiece (in parallel), falling back to the hub's fetched `refs/remotes/<machine>/task/<id>` for a machine that does not answer, or for every machine with `live=False` (read-only displays: no ssh); `decide()` is the shared rule (one place, two places is a conflict, none is no location), and `placed()` gives the project as one task sees it -- a copy carrying `"machine"`, the key `remote.py` and `remote_harvest.py` dispatch on. A project with no `machines` is on the hub without a command run. Then which machines a project may stop listing (`machines_removal_refusal` for Settings, `stranded_checkouts` for startup and `--check`) and `--check`'s per-machine part (`doctor_check`). Nothing here runs with no `machines` configured. Subprocesses go through the patchable `run_ssh` boundary |
| `remote.py` | The life of a remote task (doc-3, task-207), for a task placed on a machine (task-224: `spawn.placement()` picks it from the request's `machine`, the task's derived location and the project's `defaultMachine`; every other route takes the project as `machines.placed()` gives it at the task's location): `spawn_on_machine()` first runs a declared `workerCheck` at a pushed pinned base in a detached worker scratch tree with links, refusing before claim on failure. `doctor_worker_readiness()` uses the existing worker base ref, named by the hub checkout's current branch, without pushing. `spawn_on_machine()`/`resume_on_machine()` (reached from `spawn.spawn`/`spawn.resume`, under the same launch lock) claim on the HUB's main as usual, push that main to the worker's parked project repository (`<repoRoot>/<project>/.git`), cut or reuse `<worktreeRoot>/<project>-<task>` there with the project's `worktreeLinks` (sources under `<repoRoot>/<project>/`), resolve the agent CLI to its full path by an interactive shell on the worker, add the interactively resolved backlog/claude/codex directories to the worker login PATH for both the tmux session and its first process (task-232), probe that worker's codex for hook trust, and start the session in ONE tmux call carrying a spawn id; a launch whose answer never came back is "outcome unknown" (504) and is never re-sent or rolled back, while a definite failure rolls back only what that spawn created. `end_session()` kills the task session and every window of it linked elsewhere (by window id), then sweeps leftover processes by worktree on the worker (a `python3 -c` walk of /proc that spares Codex's app-server daemon by executable path); `cleanup()` decides "merged" on the hub against a fresh fetch and removes the worktree and branch on the worker only at that verified tip. `after_refresh()` fetches every reachable worker's `task/*` into `refs/remotes/<machine>/task/*` on each board refresh (its age is `fetch_status()`), and retries owed teardowns: an End session or cleanup the worker did not answer is recorded in `CENTRALE_OWED_TEARDOWNS` (default `$XDG_STATE_HOME/centrale/owed-teardowns.json`, present only while something is owed), bound to what was asked, and settled when the machine answers again or before that task's next spawn. Every worker command goes through `machines.run_batch`, every hub git call that talks to a worker through the patchable `run_hub_git`. Never imported without machines |
| `remote_harvest.py` | Merge gates, Discard and Abandon for a task on a machine (doc-3, task-208), reached from `harvest.evaluate_branch`/`harvest.harvest_branch` and the throwaway routes when the task's derived location is a machine (task-224; a task in two places is refused before either is reached). `survey()` reads the worker in ONE round trip (its sessions, `$HOME`, the project repository's checkouts, the branch tip, the worktree's `git status --porcelain`); `fetch_pinned()` fetches the branch into `refs/remotes/<machine>/task/<id>` and refuses anything but the surveyed tip. Gate 2 reads that pinned commit through a detached snapshot on the hub; gates 4 and 5 run on the worker in a throwaway detached worktree cut at the hub's base commit, pushed and pinned first, with checkCommand under `timeout -k` there and a sweep of the scratch directory afterwards. `harvest_locked()` re-reads the base and the worker's tip (`pinsUnchanged`), merges the pinned commit on the hub, pushes `main` back and removes the worktree and branch on the worker (owed when it does not answer). `throw_away()` cuts Discard's recovery tag on the hub from the fetched tip before removing anything on the worker, with a compare-and-delete of the branch there. `refresh_reads()` adds each project's worker checkouts to the board refresh's one round trip, and `board_placement()` turns that round trip, the hub's own branches and worktrees and the fetched refs into each task's location and `branchCheckout` with no further command (task-224). Never imported without machines |
| `codex_sessions.py` | Codex's verified conversation lookup (task-195), stdlib-only and importing nothing from this repo, so the same code answers on the hub (`spawn.codex_conversation_id`) and on a worker, where `remote.py` runs its source as `python3 -c` and its `__main__` prints the Codex home and UUID (task-207) |
| `remote_status.py` | Agent status from remote machines (doc-3, task-209). A worker's agent cannot reach this server (no reverse tunnel): its hooks -- `server.hook_argv`'s `"tmux"` sink, the same generated command as the local `"http"` one -- write `<state> <firedAt>` into its own pane's `@centrale_state` option. `Connection` is ONE `tmux -C` control connection per worker over the machines ssh configuration, attached to the persistent session `CONTROL_SESSION` (`centrale_control`); a scan (`list-panes -a`) links each task window into it (`link-window -d`, since tmux 3.4 reports no subscription for a pane in another session) and subscribes its pane by id, with the identity options (`identity_commands`, set at spawn) in every value so a reused pane id is never believed. Scans run on connect and every reconnect, on `%sessions-changed`, on `rearm()` (spawn and resume) and every 30 s as a safety net. Each change enters `server.record_agent_event` with the hook's `firedAt` and the clock skew `--check` verifies; codex corroboration captures the pane over the same connection (`server.remote_pane_capture`). `start()` runs in `main()` before any announcement: every worker is read within a bounded wait and its states restored as they stand (`server.restore_remote_state`, decision-5 as amended); `announce()` then publishes them, and an unreached worker as unreachable. Nothing starts without `machines`. The process goes through the patchable `open_control` boundary |
| `version.py` | `__version__`, and nothing else — the one place the version number is written down (task-107). Imports nothing and does nothing at import, so `scripts/release.sh` can read it out of a bare staged snapshot to derive the release tag; `server.py` imports it for what `--check` and the sidebar footer report; `code_drift()` there compares that boot-time version with `HEAD` on every board request and reports `codeDrift` (task-128), which `--check` relays from a running server through the `probe_running_server` boundary. See [Versioning](operations.md#versioning) |
| `centrale_notify.py` | The agent-lifecycle hook helper behind the session dots (task-37). Injected transiently onto a spawned session -- as inline Claude Code `--settings` JSON, or codex's `-c` overrides -- never written into the user's home config or the target repo. Run with one of `working`, `waiting`, `finished`, it writes `<state> <firedAt>` to its own tmux pane's `@centrale_state` option when `TMUX_PANE` is set (task-223), then POSTs that same state and timestamp to `$CENTRALE_EVENT_URL` (`spawn.event_url`) for `POST /api/agent-event`. Pane writes go through a bounded subprocess boundary and failure does not prevent HTTP delivery. Every failure -- bad argv, unset URL, network error, timeout -- is swallowed silently, because a hook must never break or hang the agent it is attached to. When the server is unreachable a detached child retries for a few seconds (task-201) while the hook itself returns at once; the event's `firedAt` lets the server drop a retry that a later event overtook. It also names the hook point that fired (the hook's own `hook_event_name`, else the event it was registered for) as `hook`, kept on the agent event and in the fleet journal row (task-226) |
| `static/index.html` | The frontend HTML document shell (single page, no framework/build step) |
| `static/*.js` | The frontend JS, one file per concern, each loaded by its own plain `<script src>` at the end of index.html's body in a fixed order (task-83 extracted it from index.html, task-89 split it up) — see [The frontend files](#the-frontend-files) |
| `static/styles.css` | All the frontend CSS, loaded by a plain `<link>` from index.html (task-82) |
| `static/favicon.svg` | The tab icon, declared by a `<link rel="icon">` in index.html and also served at `/favicon.ico` (task-90) |
| `static/agent-guide.md` | Agent operating guide, loaded with the server code at startup and served as plain text at `/api/agent-guide`, with examples using the bound port. Projects receive only its URL in marked instruction blocks (task-175) |
| `projects.json` | User config: port, worktree root, projects, agents map, browser ports |
| `tests/` | `test_system_status.py` (resource readers, parser, cache, unreachable handling and behavioural metric rendering); The default `unittest` suite, one module per concern -- `test_server.py` (config loading, board aggregation, sessions, the agent-event store and hook injection, the HTTP routes and their request gate, and the source-shape contracts over `static/`); `test_spawn.py` (worktree/branch resolution, agent selection, the claim commit, resume and reconcile, the spawn/resume validation refusals); `test_fleet.py` (durable history, retention, corrupt records, lifecycle observations and fleet HTTP snapshots); `test_orchestrator.py` (the waiting API: live HTTP wake-up, cursor replay, project isolation, timeout, hook deduplication and harvest notifications); `test_harvest.py` (each merge gate, branch evaluation, the harvest lock and progress record, auto-harvest, `/api/harvest`); `test_settings.py` (the whitelisted read/write of `projects.json`, the Agents editor, `/api/settings`); `test_browser.py` (per-project `backlog browser` launch, reuse, relaunch, the listener-pid registry and its register-time reconciliation, and the board-version comparison); `test_frontend_behaviour.py` (what the frontend DOES, driven under `node`); `test_source_contract.py` (the source-text tier's own machinery, the source-text ceiling, and the three censuses over this chapter -- that every module here is named in this table (task-131), that every top-level `*.py` module has a row of its own in it (task-150), and that [The frontend files](#the-frontend-files) lists the `static/*.js` files in load order (task-141)); `test_doctor.py` (the `--check` doctor's pass/warn/fail verdicts and its CLI wiring); `test_version.py` (the one version constant, how the server resolves it, and the tag `scripts/release.sh` derives from it); `test_centrale_notify.py` (the `centrale_notify.py` hook helper's argv gating, its POST to `/api/agent-event` and its bounded background retry while the server is down); `test_status_page.py` (the read-only status page: its config and Settings fields, the generated key and its constant-time check, the listener's route-by-route allowlist, path tricks, 405s and key 404s, that no response carries pane, message, settings or error text, that its snapshot never arms the reply box, the links, and a failure to listen leaving main() serving -- task-212); `test_machines.py` (remote machines, task-206: the `machines` config and a project's `machines` and `defaultMachine`, the generated ssh config, the run-on-machine transport's quoting, worker-side `~`, framing and three-way results -- the worker script run under a local `sh`, never ssh -- one batched round trip per machine per board and fleet refresh, where a task is (task-224: worktree, then branch, a conflict named with both, an unreachable machine by its fetched ref), refusing to take a machine from a project that still has tasks there, `--check` per machine, and that no ssh process starts without machines); `test_placement.py` (per-task placement, task-224: spawn's `machine` field and its default, the allowed list, a task's location winning over a request elsewhere, a project's cap counted across machines, and one project with a task on the hub, one on a worker and one in both places driven through every per-task route and the board); `test_placement_ui.py` (per-task placement in the UI, task-225, under `node`: the machine picker beside Spawn on the card and in the drawer and the `machine` it sends, Settings' machines and default machine and the server's refusal under them, a conflicted location on the card, drawer and session row with its actions disabled, and a Fleet card per place); `test_remote.py` (the life of a remote task, task-207: spawn, resume, End session and cleanup on a machine against a faked worker -- the claim on the hub before the push, the launch by full path with the worker's codex probe, outcome unknown never re-sent, rollback, per-project and per-machine caps, owed teardowns and their binding, the task/* fetch and its age on the board); `test_remote_harvest.py` (merge gates, Discard and Abandon for a task on a machine, task-208, against a faked worker: a live session, dirty worktree or external checkout there blocks, task done read from the pinned fetched commit, the trial merge and checkCommand sent to the worker at the pushed pinned base, a moved tip or base refusing the merge, the recovery tag cut on the hub before anything is removed, stale confirms and unreachable workers destroying nothing, and the board's branchCheckout from the refresh round trip); `test_remote_ui.py` (remote sessions in the sessions list and Fleet, task-210: machine and worktree fields, per-machine capacity, unreachable distinct from unknown, nothing added without machines), `test_remote_status.py` (agent status from remote machines, task-209: the one generated hook command and its two sinks -- the worker snippet run under a local `sh` against a stand-in `tmux` -- and the single-machine before/after check, which replays `single_machine_events.py`'s scripted hook sequence and compares it with `fixtures/single_machine_events.json`, captured from the code before task-209); `test_restart_state.py` (what survives a restart: journal-restored badges bound to the same tmux session instance, the boot-time orchestrator announcement, and `firedAt` ordering of retried hook events -- task-201); `test_delivery.py` (`POST /api/deliver`'s deliver-and-confirm -- the dialog refusal, the echo match and its baseline, each failure outcome -- and the delivery log behind `GET /api/deliveries`, over real claude and codex pane captures in `fixtures/panes/`); `test_parked.py` (PARKED: `parked_status`'s idle threshold, dialog and undelivered-message evidence and the finished-agent negative, over the same real pane captures, plus the `parkedAfterSeconds` key and the sessions and Needs-you surfaces -- task-170.3); `test_scan_release.py` (`scripts/scan_release.py`'s secret and identity scan, including a clean run over this repository); `test_release_snapshot.py` (what `git archive HEAD` ships: source in, `backlog/` out, no generated content); `test_docs_operations.py` (docs/operations.md's hand-kept tool and allowlist enumerations, pinned to release.sh, `tests_integration/` and the scan -- task-145); `test_docs_references.py` (the one rule over every document at once: a path under `tests/` or `tests_integration/` named in the public prose or a top-level module has to resolve to a file, after version.py cited a test that never existed -- task-163); `test_screenshots_script.py` (the one property `scripts/screenshots.py` may not lose: its faked CLI boundaries refuse any directory outside the throwaway sandbox, and no command-line option can aim it at a real board -- hermetic, and Playwright-free like everything else here -- task-153); `test_integration_base.py` (the unit-tier test OF `tests_integration/base.py`: its pure `parse_created_task_id` helper, `require_tools`' skip-or-fail switch on `CENTRALE_REQUIRE_INTEGRATION`, and the per-run tmux socket name and its path -- all decided before any subprocess runs, so it runs no integration test and launches nothing); `test_integration_app_modules.py` (the guard on `tests_integration/base.py`'s hand-kept `APP_MODULES`: every top-level module `server.py` imports, transitively, has to be in it, so a missing one fails here rather than at the release gate -- TASK-198) -- plus recorded JSON fixtures and pane captures in `fixtures/` and shared helpers: `source_contract.py` (lazy, self-describing slices of `static/`) and `js_harness.py` (the DOM shim the frontend is driven over under node), and `lifecycle_harness.py` (manual timer and settled-pane boundaries for lifecycle tests). Every `test_*.py` here must be named in this row: `test_source_contract.py` fails the suite otherwise |
| `tests_integration/` | The opt-in second tier: real `git`/`tmux`/`backlog` processes in sandboxes, never collected by `discover tests`. Its module list lives in [tests_integration/README.md](../tests_integration/README.md) ("What's covered, and where") and nowhere else, so it has one place to drift from; see [Testing](#testing) |
| `scripts/` | The tooling that is run by hand or by a release rather than by the server: `release.sh` (builds and publishes the curated public snapshot, and runs the release gate over it), `scan_release.py` (the secret/identity scan that gate runs first -- also runnable with no arguments over this repo), `harvest.sh` (a standalone report on, and optional cleanup of, merged Centrale worktrees), and `screenshots.py` (regenerates docs/img's four images from the synthetic world committed beside it in `screenshots_fixture.json` -- run by a maintainer after a UI change, by nothing else, and it cannot be aimed at a real board; task-153). One row for the directory: see [Releasing](operations.md#releasing) for what a release runs, in order |

### server.py

Everything shared lives here:

- **Config** — `load_config()` reads `projects.json`, expands `~`, applies
  defaults, and normalizes the `agents` map to one canonical shape
  (`{name: {"cmd": [...], "promptSuffix": ..., "resumeCmd": ...}}`) via
  `normalize_agents_map`. Malformed agent entries raise `ConfigError` at
  startup.
- **Subprocess boundaries** — small, patchable functions (`run_backlog`,
  `run_backlog_raw`, `run_git`, `run_tmux`, `which`,
  `launch_browser_process`) are the only places subprocesses run. All calls
  use argument lists (never `shell=True`); tests patch these to stay hermetic.
  Backlog commands, including the detached browser, are prefixed with
  `nice -n 10` when `nice` is on PATH, otherwise run directly (task-196).
  No `preexec_fn` runs in this threaded server.
- **Board aggregation** — `get_board()` fans out per project on a
  `ThreadPoolExecutor`, reading each task's `ready` flag from the `isReady` that
  `task list --json` carries (backlog's own answer; a list without it is an
  error, not "nothing ready"), so a project costs one `task list` run plus,
  when a task has a milestone, the milestone-title read. Successful CLI inputs (including
  milestone titles) are reused per checkout only while recursive `backlog/`
  file metadata stays unchanged. Per-checkout locks coalesce simultaneous
  clients; metadata is checked before and after loading, and failures are
  retried. Lifecycle/git enrichment runs on every request. A failing project
  becomes an `error` field on that project, never a crash. See the caching
  exception in the manifesto (task-196).
- **Sessions** — `list_sessions()` parses `centrale-*` tmux sessions (a
  missing tmux server means an empty list); `enrich_sessions_with_files()`
  maps each session name back to its worktree and attaches the files touched
  so far.
- **HTTP** — `Handler` on a `ThreadingHTTPServer` serves `static/` files and
  the JSON API: `GET /api/board`, `GET /api/task`, `GET /api/sessions`,
  `GET /api/session-pane` (one `tmux capture-pane` of a live session's
  rendered text for the drawer's live pane; refuses with 403 when
  `sessionPreview.mode` is `"off"`), `POST /api/session-input` (one
  validated line by `tmux load-buffer` + `paste-buffer -d -p` (bracketed
  paste) + `send-keys Enter`, into that same exact-match session; refuses
  with 403 unless the mode is
  `"interact"`, and with 409 unless this process captured that pane within
  the last 10s), `POST /api/deliver` (task-170.2: the same paste path for
  a program messaging an agent, bracketed by its own captures -- a dialog
  on the pane refuses before the paste, and success needs a new echo line
  afterwards; every attempt is appended to the JSON-lines delivery log
  that `GET /api/deliveries` reads), `POST /api/rule` (task-172: a ruling
  on a spawned task -- delivered through that same path to a live agent,
  or committed on the task branch by `spawn.commit_ruling` when none is
  live), `POST /api/spawn`, `POST /api/browser`. Errors are JSON `{"error": ...}`;
  tracebacks never leak. `_refuse_untrusted_request()` runs at the top of
  **both** `do_GET` and `do_POST`, before any routing, so no endpoint can
  forget it and a refused request reaches no `git`/`tmux`/`backlog`/
  `checkCommand`/scratch-worktree/pane-capture boundary (task-99,
  task-122): `Host` must be a loopback name (`host_is_own()`) at
  `server_address[1]` — the whitelist that refuses DNS rebinding, where the
  attacker's hostname resolves to `127.0.0.1` and the browser calls their
  page same-origin; `Sec-Fetch-Site`, when the client sends one, must be
  `same-origin` or `none` — the browser-set, page-unforgeable header that
  refuses a direct cross-site `<img>`/form aimed at the loopback address;
  and an `Origin`, or absent that a `Referer`, must be one of this
  process's own `loopback_origins()` (403 for each), built from the bound
  port rather than the request's `Host`. A fourth, `POST`-only, follows:
  the body must be declared `Content-Type: application/json` (415
  otherwise — the content types a cross-origin HTML form can produce are
  exactly the ones this excludes). Reads are inside the boundary because
  reads are not side-effect-free: `GET /api/harvest` runs the whole merge
  gate (scratch worktree, real merge, the project's `checkCommand`) and
  `GET /api/session-pane` arms `POST /api/session-input`. State-changing endpoints
  take their identity from the JSON body only; `POST /api/agent-event`
  keeps query identity as the documented `CENTRALE_EVENT_URL` exception. `main()` also runs `detect_capabilities()` so a
  machine without tmux still serves the board with spawning disabled.

- **Lifecycle serialization** (task-121) — `task_lifecycle_lock(project,
  taskId)` returns one lock per task, and every route that creates or
  destroys a task's worktree or branch runs under it: `POST /api/spawn`,
  `/api/resume`, `/api/cleanup-branch`, `/api/discard-attempt`,
  `/api/abandon-worktree` and `harvest.harvest_branch`. It is per task rather
  than global so a slow merge of one task never stalls a discard of another;
  harvest's cross-task lock is `_harvest_lock`, and the order is
  fixed — `harvest_branch` takes `_harvest_lock` *then* the per-task lock, and
  nothing holding the per-task lock ever asks for `_harvest_lock`, so there is
  no cycle. Spawn and resume also take a per-project launch lock inside
  the task lock, covering the fresh tmux count through session creation
  (task-177); a launch never asks for another task lock. This prevents two
  tasks from both taking the last `maxAgents` slot and serializes writes to
  the project's link exclusions. Read-only routes take none of these locks.
  The destructive throwaway routes
  additionally require the state their confirm described
  (`expectedBranchTip`, `expectedDirtyPaths`, straight from `GET
  /api/discard-preview`) and refuse with 409 if a fresh survey under the lock
  disagrees — see [docs/api.md](api.md#post-apidiscard-attempt).

`spawn.py`, `harvest.py`, `settings.py` and `browser.py` import `server` for
these boundaries and are themselves imported lazily at their call sites,
avoiding a circular import at module load. They call `server.run_git(...)`
through the module object so a single patch is seen everywhere. Started as
`python3 server.py`, the running module is `__main__`, so server.py
registers itself in `sys.modules` as `server` before anything can import
it (task-214); otherwise each of those imports would load a second copy
of server.py with its own empty fleet journal, agent events, lifecycle
locks, caches and exception classes. The hermetic suite imports `server`
by name and cannot see that split; `tests_integration/test_status_page_integration.py`
runs a real process to.

### spawn.py

Validation (`project` must be configured; `taskId` must match
`^[A-Za-z]+-[0-9]+(\.[0-9]+)*$`), agent resolution from the task's first
assignee (`resolve_agent`, falling back to `defaultAgent`), and the spawn flow
below. Failures raise `SpawnError` carrying the HTTP status (400/404
validation, 409 duplicate session, 503 no tmux, 500/502 git/tmux failure).
`CENTRALE_SPAWN_CMD` overrides the launched command so tests never start
a real agent. `checkout_state()`
(task-70) classifies where a task branch is checked out from one `git
worktree list --porcelain` — `centrale`, `external` (a foreign worktree,
with the branch's last-commit age), or `none` (parked) — feeding the board's
`branchCheckout` field, harvest's honest gate-2 reason, `GET /api/task`'s
choice of where to read `branchTask` from (task-79), and the 409 with
which `spawn()`/`resume()` refuse an externally checked-out branch before
any side effect. `detached_snapshot()` is the shared context manager for
reading a branch Centrale must not check out: a throwaway detached worktree
under the `TMPDIR`-overridable temporary directory, always removed
(registration and directory) on every path, used by both harvest's gate 2
and `GET /api/task` for a parked branch.

The task-file lock (task-172) lives here too, as plain filesystem state
with nothing recording it: `lock_task_file`/`unlock_task_file` flip the
write bits of `backlog/tasks/<id> - *.md` in a checkout
(`task_file_path` finds it by Backlog.md's filename convention),
`locked_task_files` reads them back for the board's `taskFileLocked`, and
`release_stale_task_locks` unlocks any with no `task/<id>` branch behind it
(run by `main()` and `--check`). `commit_ruling` is `POST /api/rule`'s
no-agent case: a `backlog task edit --comment` in the Centrale worktree,
committed with `git commit --only` scoped to that one file.
For a task placed on a worker, `remote.commit_ruling` performs that same
scoped edit and commit in the worker's worktree; live rulings use the
worker's tmux and subscribed pane capture (task-230).

### harvest.py

`evaluate_branch()` reads task completion from the task branch and runs the
five merge gates without mutating the project checkout. It uses the managed
task worktree when present. For a parked branch it reads through
`spawn.detached_snapshot()`, letting the Backlog CLI locate/read the task in
that scratch tree; a foreign or stale Centrale checkout still refuses. `worktreeClean` re-enumerates git
worktrees fail-closed and passes vacuously only for a still-parked branch, with
an explicit reason.
`harvest_branch()` serializes action under one global lock plus the task's own
lifecycle lock (task-121, see server.py above), re-evaluates those gates, checks main-checkout overlap, and merges only when every gate passes.
If main says Done while the branch does not, the report carries both statuses
and the branch's last commit subject; explicit POST action flags can adopt
Done through `backlog task edit` and commit only the branch task path. A
second, separately confirmed action may restore exactly one eligible
unstaged main-side task edit. Both actions re-verify their preconditions and
re-run the ordinary gates; they add no stored state and never broaden the
existing gate semantics. When the merge/check gate fails on a branch that
is behind its base, `evaluate_branch()` also derives the behind-count from
git (`rev-list --count <branch>..<base>`) and attaches `behindBase` plus a
collision hint; the drawer turns that into a "Resume to reconcile" offer
that is `POST /api/resume` with `reconcile: true` — the same
`spawn.resume()` path and the same resume-command tiers, with a reconcile
prompt appended as the trailing argument (merge base into the branch, fix,
check, commit; never merge to base), so the conversation that built the
branch is continued rather than replaced. Centrale merges or rebases
nothing itself there.
After a successful parked merge, cleanup skips only the absent managed
worktree and deletes the branch with `git branch -d`; the safe-delete refusal
remains a non-fatal cleanup warning and is never upgraded to `-D`.

While an attempt runs, `harvest_branch()` publishes the stage it is entering
to one in-memory record next to the events deque — the five gates via an
`on_gate` callback `evaluate_branch()` invokes as each one begins, then the
pre-merge checkout re-check and the merge itself. `GET /api/harvest-progress`
reads it, so the Merge button can say "Running tests" instead of just
"Merging" for the gate that is nearly all of a checked project's merge time.
One record suffices because the merge lock already serializes every attempt;
a `finally` clears it on every outcome, and an evaluate-only `GET
/api/harvest` passes no callback, so it publishes nothing.

### orchestrator.py

`GET /api/orchestrator-wait` sleeps on a `threading.Condition` until a
project has a notification after the supplied cursor, or the timeout expires.
`record_agent_event()` publishes transitions to public `finished`/`waiting`/
`idle` under its badge lock; codex's `idle` notification explicitly says its
turn ended and may need input. A Codex permission hook first arms a
30-second daemon timer while the badge stays working. Activity, turn-end,
clear and reset cancel it; duplicate hooks keep the original deadline.
At expiry, a capture through `run_tmux`, outside the badge lock, must show
a dialog before the callback sets waiting and publishes one notification
together. This guards against automatically approved long tools and old
hooks whose waiting signal is stale. Without a dialog it checks again in
30 seconds; capture errors expose unknown and retry, except a gone session
ends the candidate. Timer identity is rechecked under the badge lock after
capture, so a cancelled callback cannot resurrect a wait for a new request
or session. New Codex spawns also receive `PostToolUse → working` to clear
permission candidates promptly.

Codex Stop/notify now arms a separate one-second confirmation timer
(task-192). It captures the current screen without scrollback: an
interruptible working footer retries in one second, a dialog publishes
waiting, and a settled pane publishes idle. Empty/failed captures expose
unknown and retry; missing sessions stop checking. New activity, clear,
and reset cancel pending stops, and identity checks after capture exclude
obsolete callbacks. Duplicate Stop/notify signals preserve the deadline.
Both timers feed the same lifecycle store and publication path used by
fleet/Needs-you and orchestrator-wait. Claude's helper consumes Notification
stdin and only forwards input-request types; idle reminders never reach
the state store. Genuine Claude prompts and Stop retain their timing.
`harvest._record_event()` passes the same
attempt summary it appends to `recent_events()`, including the first failed
gate. The notification stream suppresses an unchanged line per project/task
by comparing with that task's last published harvest line, including blocked,
already-merged and error outcomes. Changed lines still publish; only a real
merge always publishes even if identical. This comparison and publication
share the condition lock, and hook events never reset the harvest comparison.
The UI attempt log still records every attempt. Evaluate-only requests
publish nothing. Both producers append and notify under the condition lock,
so publication cannot fall between checking for an event and falling asleep.
Waiting releases that lock and holds no hook, harvest or lifecycle lock.

The project stream retains every compact line for this server process's
lifetime, including events before the first subscriber; unlike the UI's
50-entry harvest deque it never evicts unread events. This costs memory
proportional to notification count. Consumption is a caller-owned cursor,
not deletion: retrying a cursor replays the same answer and independent
callers cannot steal each other's events. A random per-project stream id
and increasing position reject old-process, other-project and future
cursors with 409. Restart drops notification history; at boot the new
process publishes one line per live session (its journal-restored state, or
that it is unknown) before serving, so a caller that drops its 409'd cursor
and waits without `after` hears every live session first (task-201). This is past-event delivery, never an input to task status,
merge gates or lifecycle derivation. An outstanding HTTP request owns the
condition wait. Codex permission confirmation uses at most one scheduled
timer per pending task, with no new external resource or persistent store;
the timer factory is injectable so unit tests never wait or call real tmux.
See the [API contract](api.md#get-apiorchestrator-waitprojectnameaftercursortimeoutseconds)
and the [optional orchestration loop](agents.md#waiting-for-orchestrator-events-optional).

### fleet.py

`main()` opens the JSONL journal at `CENTRALE_FLEET_LOG`, falling back to
`$XDG_STATE_HOME/centrale/fleet.jsonl` (or `~/.local/state/centrale/fleet.jsonl`).
Imports perform no journal I/O; embedded servers can inject a `Journal`.
Spawn/resume, public hook transitions, harvest attempts and session endings
append observations. Claude working state-change rows can also carry the
hook's allowlisted `tool_name`, `agent_id`, `prompt_id` and `session_id_hash`
(task-227). The last is a 12-hex SHA-256 prefix, never the raw session ID;
no tool arguments, prompt text or transcript path are recorded. The helper
and HTTP receiver share the filter in `centrale_notify.hook_provenance`.
These fields diagnose a trailing working hook's source, without changing
state decisions or deduplication. Records older than 48 hours are removed on startup,
append or snapshot reads, with atomic replacement in the same directory.
Invalid lines are counted and skipped; write errors are exposed by the API.

`GET /api/fleet` surveys tmux once, without per-session git enrichment, then
combines current ephemeral hook states, configured capacity, windowed history
and delivery failures. The additive `needsYou` field derives permission
dialogs from fresh captures of waiting workers (unless preview is off), reads
idle workers' branch reports through Backlog, and reuses `get_board(config)`'s
cached tasks across every configured project for the fixed `needs-owner-approval`
label. Labelled tasks and tasks with spawn branches get detail reads (workers can
add the label on their own branch), preferring branch reports and
otherwise reading the project task through Backlog, for the latest comment and
completion/label-removal checks. It also selects recent unsuperseded delivery failures and
labels failed merge attempts as historical signals, never current gate verdicts.
Owner questions are recomputed at most every 30 seconds
(`OWNER_QUESTIONS_TTL`), and never on a request's time after the first one
since startup, which computes inline: an expired window returns the last
result at once and starts a single background refresh thread (single-flight;
polls during it reuse the last result). A refresh that fails replaces the
result with an error, never keeps the old questions. Inside a refresh, and in
the idle-report check, a worker's `backlog task view` (which loads the whole
board, about a second on a large one) is reused while its source is
unchanged: the task's own file, located by ID on every check and compared by
path, inode, size, mtime and ctime (never parsed), or, for a branch checked
out nowhere, the branch tip commit. A file that cannot be located singly is
read fresh; failed reads are never reused. `GET /api/task`'s `branchTask`
always reads fresh. These are in-memory display optimisations (task-215),
like the board-input reuse of decision-4, not task state.
See [the fleet API](api.md#get-apifleetwindowseconds) for the filtering rules.
`GET /api/sessions` shares disappearance observation.
A failed tmux survey never becomes evidence of an ended session; survey start
times prevent an older concurrent poll from clearing a newly spawned badge.
Restored observations can establish that a previously seen session has now
ended. At boot (task-201, decision-5) a still-live session also gets its last
settled badge back -- `finished`, `idle` or `waiting`, with its original
`stateSince` -- but only when the journal row is not older than the tmux
session's creation time and any creation time the row recorded matches;
state rows record `agentKind` and the session's `created` for that purpose.
`working`, a recreated session or a missing row stays unknown until a fresh
hook arrives. History is evidence of past events, never task state,
merge authority or evidence that a session is live. No polling thread is added.

### browser.py

`launch_or_reuse()` starts a detached `backlog browser` process per project on
a stable port (`browserPortBase` + project index, or per-project
`browserPort`), reuses it while alive, and terminates all of them atexit.
`version_drift()` then asks the board that was opened what version it is
running (`/api/version` on that same port) and compares it with `backlog
--version` on `PATH`, so the UI can say when a board is behind the CLI --
reported, never acted on.

### The frontend files

Twenty-one plain `<script src>` files, loaded in this order at the end of
index.html's body — no build step, no bundler, no modules:

| File | Concern |
|---|---|
| `state.js` | The shared board/session/UI state, and the localStorage view-state helpers |
| `dom.js` | The tiny DOM builder (`h`, `byId`, `clearChildren`) and the per-project colour helpers |
| `api.js` | The `/api` fetches, the auto-harvest event toasts, and the refresh/countdown loop |
| `tasks.js` | Pure task/session helpers: sorting, columns, filtering, the milestone dropdown (task-86, project-scoped and title-labelled by task-91), session lookup, agent badges |
| `feedback.js` | Clipboard, toasts, and the Backlog.md browser launcher |
| `board.js` | The sidebar chips, the error banners, and the board/column/card render |
| `spawn.js` | The Spawn/Respawn/Resume buttons and their POSTs |
| `harvest.js` | The gated merge, the post-merge branch cleanup, and End session |
| `sessions.js` | The sidebar's live-session panel |
| `drawer.js` | The task drawer: detail, session, spawn and harvest areas |
| `pane.js` | The live tmux pane preview, its reply row, the theater, and drawer close |
| `shell.js` | The header/search/keyboard wiring, the theme toggle, the sidebar collapse |
| `settings.js` | The settings modal, including the agents editor |
| `fleet.js` | The one shared poller of `GET /api/fleet`: views subscribe and are handed each snapshot; one timer, running only while someone is subscribed (task-187) |
| `views.js` | The view registry (`C.views.register({id, label, render[, focus, badge]})`; an optional `badge(snapshot)` puts a live value on the tab, through the registry's own fleet subscription held only while a visible view has one), the tab strip built only from registered, un-hidden views, cross-view `open()` that no-ops on an absent or hidden target, and Settings' Views checklist, persisted in localStorage as hidden ids. Each view is its own `static/view-*.js` with one script tag between `views.js` and `main.js`; nothing else names a view (task-187) |
| `shared-pulse.js` | The fleet-at-a-glance bar above every view but the Board: live agents, merges, and a stacked state bar whose working segment shimmers and waiting segment marches. Registers with `C.views.registerShared({id, slot: "top", render})`; deleting the file and its tag removes it (task-194) |
| `shared-feed.js` | The Activity feed beside every view but the Board (`slot: "side"`, stacking below at narrow widths), drawn from `snapshot.history`, newest first; removable the same way (task-194) |
| `view-needs-you.js` | Optional Needs you inbox: derived attention signals, inline permission answers through the shared pane key sender, and a live tab count (task-188) |
| `view-fleet.js` | The Fleet view: one card per project with used/max agent slots ("no limit" when `maxAgents` is unset), each live agent's state, time in state, name and a two-hour sparkline; clicking an agent asks the registry to open it on the Timeline, a no-op when that view is absent or hidden (task-189) |
| `view-timeline.js` | The Timeline view: every agent active in the window as a row of state segments grouped by project, merge markers, a now line and a hover tooltip; it scrolls forward because each snapshot carries a newer `now`. Segment derivation is `C.fleetModel` in `fleet.js`, shared so neither view file needs the other (task-189) |
| `main.js` | The top-level render orchestration and the boot call |

One more script, `phone.js`, is deliberately not in that table: index.html
never loads it. It belongs to `static/phone.html`, the read-only status page
`status_page.py` serves on its own listener (task-212), which loads only
`dom.js`, `fleet.js` (for `C.fleetModel`; nothing there subscribes, so its
poller never runs) and `phone.js`, styled by the same `styles.css`. It polls
`/api/status` with the key from its own URL and draws the summary bar, the
project cards, Needs you and Activity with the Fleet view's classes, and has
no controls.

**The seam.** Each file is one IIFE taking `window.Centrale` as `C`, and that
object is the only thing they share. Two rules (repeated at the top of
`state.js`):

1. A shared value that gets **reassigned** lives on `C` itself
   (`C.boardData`, `C.currentDrawer`, `C.sessionPreviewMode`, ...) and is read
   and written as `C.x` everywhere, including in the file that owns it — a
   local plus a published alias would go stale the moment another file
   assigned to it.
2. Everything else shared — functions, constants, containers only mutated in
   place — stays an ordinary declaration and is published at the end of its
   own file (`C.h = h;`). Other files reach it as `C.h`, always late-bound, so
   no file has to be loaded before another for a call to resolve.

A name used in only one file stays a plain local (about half of them do);
promoting one is two lines. The order above therefore matters in one
direction only: a few files run statements as they load (state.js reads
localStorage, pane.js/shell.js/settings.js attach listeners, main.js fires
the first refresh), so state.js and dom.js come before those and main.js
comes last. Otherwise the files keep the order their sections had inside the
single app.js they were split out of, and the split is a pure move: nothing
was renamed, reordered or rewritten beyond that seam.

The frontend is tested in two tiers, both without a browser dependency
(task-108). The contract tests in `tests/test_server.py` grep these files
for **source-shape** invariants -- that the seam publishes no name twice,
that the pane fetch has exactly one home, that index.html loads each file
once -- each loading what it asserts on through `tests/source_contract.py`,
whose slices resolve inside the test that reads them so a renamed
function fails readably rather than erroring out of a fixture.
Claims about what the frontend **does** live in
`tests/test_frontend_behaviour.py`, which runs the real sources under
`node` over the small DOM shim in `tests/js_harness.py` and observes the
result -- how many timers are armed, which URLs are fetched, where a node
ends up in the tree, what a key press closes. `node` is optional: that
module skips whole on a machine without it, and `python3 -m unittest
discover tests` still reports OK. A release is the one exception -- see
[Testing](#testing) below.

### static/index.html + static/*.js

Kanban board grouped by status with project/ready/search filters, a task
drawer (description, acceptance criteria, dependencies, plan/notes) with the
Spawn button, and a sessions panel with attach commands. Polls `/api/board`
and `/api/sessions` every 10s. While a drawer is open for a task with a live
session, a single separate poll of `/api/session-pane` updates only the
drawer's live-pane element — never a board re-render — and stops when the
drawer closes, the session ends, or the setting is turned off. That one
poller's *interval* follows what is happening (task-114, `paneTickDelayMs`):
~300ms for a few seconds after a reply is accepted, so the sent line and
the agent's first response arrive as one motion rather than one jump; 1s
while the session's agent badge says `working`; the 2s baseline otherwise.
The burst is an expiry, not a mode — it decays on its own and is cancelled
at once by the theater closing or the poll stopping — and it re-arms the
existing timer rather than adding a second one. In the
`"interact"` tier that same element also carries the reply row (one-line
text input + Send, plus the task-135 Esc / Enter key buttons that POST a
`{"key"}` body instead of a `{"text"}` one), which POSTs to
`/api/session-input` and is disabled,
with the reason and the capture age shown, whenever the last capture is
missing, failed, or older than 10s. The pane section's header holds an
Expand/Narrow toggle: a `wide` class on the (position:fixed) drawer widens
it to fit the 80-column grid without horizontal scroll and gives the
capture a taller max-height. The preference is per-viewer localStorage
(`centrale-drawer-wide`, fault-tolerant, never `projects.json`), applied
only while the pane section exists, and its writer touches nothing but
the drawer's class and the toggle — never a board render or lane scroll.
Beside it, a Maximize button opens the *session theater*: the settings
modal's skeleton (a fixed dimmed `#theater-backdrop` plus a fixed,
centered `#theater` dialog layer, ~90vw × 85vh, `role="dialog"`, `.open`
classes) into which the existing `#drawer-pane-area` node is *moved* —
header, age label, capture, error line and, in the `"interact"` tier, the
reply row, which docks at the bottom. Nothing is copied, so the one pane
poller and the reply code keep writing to the same element ids and the
poll cadence, reply gate and tier rules are untouched by construction;
`"view"` has no reply row to move and `"off"` has no pane section, hence
no theater. Maximize always opens on the newest output, at the bottom of
the capture: the same node is re-rendered from a 40-line tail to the full
200-line window, so a pixel offset taken in one points somewhere unrelated
in the other, and reading what the agent is doing *now* is what the control
is for. Closing (Esc, backdrop click, or the header's Close button)
moves the node back to its drawer slot and restores the drawer's own
capture and body scroll offsets — never the theater's, which is the same
broken mapping in reverse; `stopDrawerPanePolling()` closes the theater
before emptying the area, so a session ending or the tier turning off
returns the user to the drawer. The single global Escape handler closes
one layer at a time, outermost first: settings, then theater, then drawer.
Open/close touch only the overlay's classes, the node's parent, those
scroll offsets and the toggle — never a board render or poll state.

Beside the pane, the theater carries a *task rail* (task-93): a ~320px
left column with the open ticket's read-only detail — id and title,
status (plus a `branch: …` chip when the agent's branch says something
else), milestone, labels, description, and the acceptance criteria read
from `currentDrawer.branchTask` when there is one (labelled "agent
branch"; "main" otherwise), so the ticks move as the agent works. It is
deliberately additive and revertible: its own block in `pane.js` reached
by exactly two call lines in `openTheater`/`closeTheater`, its own CSS
block keyed on `.has-rail`/`.rail-collapsed` (classes that exist only
while the rail node does), its own localStorage key
(`centrale-theater-rail-collapsed`), and no markup in `index.html`. It
renders from what the drawer already fetched — `openDrawer` stashes the
`/api/task` response as `currentDrawer.detail` next to `branchTask` — so
it adds no fetch, no timer and no poll state, and the single-poller
contract is untouched. A "Hide task"/"Show task" control in the pane
header (the slot the theater-hidden Expand/Narrow toggle occupies)
collapses it out of the layout so the pane gets the full theater width;
below 1000px of viewport width the rail collapses itself and the control
says why. No lifecycle actions live there: the theater reads and
replies, the drawer acts.

The drawer has two data sources with two lifecycles. Everything derived
from board data — the spawn, harvest and session areas, the header's
status, the external-checkout state — follows the ordinary
`/api/board` tick: `syncDrawerSummary` re-points `currentDrawer.summary`
at the freshly fetched board task and those areas re-render. The drawer
*body* — description, acceptance criteria, dependencies, plan and notes,
plus the `branchTask` stashed behind them — is `GET /api/task`, which
the board response does not carry. Until task-126 it was fetched exactly
once, on open, so a criterion an agent ticked while you were reading the
drawer stayed unticked until you closed and reopened it. It now rides
the same tick: `doRefresh` calls `C.refreshDrawerDetail()`, one extra
`/api/task` per tick for one task, and only while a drawer is actually
open — with none open the call returns before it fetches, which is also
all "the refresh stops when the drawer closes" needs to mean. It arms no
timer, so the single-poller contract is untouched, and the open and the
refresh share the one `fetch("/api/task?")` call site in the frontend.
Not disturbing a viewer mid-read is the constraint that shapes the rest:
a response whose JSON is byte-identical to what is already rendered
returns before touching any DOM (the same rule
`renderBoardAndSessionsIfChanged` follows for the board — a rebuild
would re-fold sections the viewer just opened, reset scroll and drop
focus); a response that lands after the drawer closed or moved to
another task is discarded on the `drawerRequestSeq` it was sent under;
and a refresh that *fails* says nothing at all, leaving the last good
body on screen, since the viewer is mid-read and the next tick will try
again. The open-time loading placeholder and error message stay the
open's alone. Merge gates are not affected either way: they read the
branch-side task fresh from disk on every harvest evaluation, so this
was always a display gap and never a correctness one.

The tick that *does* carry a change still has to rebuild the body, and
that rebuild is what would reset scroll and drop focus (task-127). So it
carries the viewer across: `renderDrawerDetail` reads the body's
`scrollTop` and what has keyboard focus before it clears, and puts both
back afterwards — the offset clamped to the new content height, so a
ticket that shrank settles at its new bottom instead of holding an
offset that no longer exists. This is the second half of the pattern
`renderBoard` uses for the Kanban lanes (task-50), on one scroll
container rather than several. Focus is keyed by something that survives
the rebuild — a section toggle by its section's `data-section`, a linked
dependency row by its `data-dep` — and where the element it was on is
gone, focus lands on `#drawer-body` itself (`tabindex="-1"`, ring
suppressed) rather than falling back to `<body>` and out of the drawer
entirely. Focus that was never in the body is not touched. The
preservation is the *refresh's* alone: an open, and a switch to another
task, render at the top with no focus restored.

The drawer's summary body (task-97) builds every section through one
`drawerSection()` helper in `drawer.js`: an `<h3>` whose whole row is a
button (caret, name, count), plus the section body it folds. The counts
come from what is already rendered — checked/total for acceptance
criteria, a length for dependencies, a word count for the free-text
sections — and the fold default from the section's size, with an
explicit viewer toggle stored per section key
(`centrale-drawer-sections`, same fault-tolerant per-viewer rules as the
two preferences above) taking precedence. The rebalance that goes with
it is one CSS ceiling: `#drawer > #drawer-pane-area:not(:empty)` is
capped at `max(248px, 30vh)` — scoped to the drawer's own child, so the
theater (which *moves* that node into itself) is unaffected, and
exempted for `.wide`, which exists to read the pane. The delicate flex
model underneath is deliberately untouched: the body keeps
`flex: 1 1000 auto` so it still yields height before the footers, and
the pane footer keeps the `overflow: hidden` that disables its automatic
minimum size plus its 168px floor, so a short window still shrinks the
capture rather than pushing the drawer's bottom off screen. Measured at
1440x900 with a live session, the body went from 105px against 1079px of
content to 303px against 303px; at 640px of window height a `max-height`
media query tightens the drawer's own vertical rhythm (padding only) and
the body goes from its bare padding to ~100px with the first three
section headers and the fade cue showing.

The settings modal (gear icon) is one scrolling form over `GET`/`POST
/api/settings`. Its Agents section (task-78) is a single `<details>`
element, collapsed on every open — the summary line reads "Agents (N
configured, default: X)" — that edits the `agents` map itself: built-in
rows (`claude`, `codex`) are read-only-named and non-deletable, user
rows editable with a confirm-armed Remove (armed, with an explanation
naming the affected board tasks and the fallback default, only when a
task on the already-loaded board is assigned to that agent or it is the
picked default), plus "+ Add agent" and the default-agent picker. Rows
are a working copy the inputs write into; Save posts the whole map as an
ordered `agents` list **only when the section was touched**
(`settingsAgentsDirty`), so an untouched save never rewrites the key.
Commands are shown and typed as one shell-quoted line (`cmdText` =
`shlex.join(argv)`; the server splits it back with `shlex.split`, so a
spaced argument round-trips as one argv element). The server's response
`warnings` (executable not on `PATH`) go to the status line and a row
tag, never a refusal. Nothing outside the modal is touched. No tabs: if
the modal ever needs a third heavyweight section, that is when tabs
happen.

## How a spawn flows end to end

`POST /api/spawn {"project": "my-app", "taskId": "TASK-2"}` →
`spawn.spawn()`:

1. **Validate** — project name and task id checked, tmux capability
   confirmed, and a 409 returned if a `centrale-<project>-<taskid>` session
   already exists, or if the project's optional `maxAgents` cap is reached.
   The cap counts only that project's live tmux sessions, never lifecycle
   badges or stored counters, and names them in the refusal.
2. **Claim** — `_claim_and_commit()` sets the task to "In Progress" via
   `backlog task edit` in the main repo, then…
3. **Commit** — commits any resulting `backlog/` change on the repo's current
   branch, so the worktree cut next already contains the claim (avoiding a
   divergent copy of the task file when the agent claims it again). Both
   steps are best-effort: failures become `warnings` in the response, never a
   blocked spawn. Then (task-172, `lockSpawnedTaskFiles`, default on) the
   main checkout's copy of the task file is made read-only, so the agent on
   its branch is that file's only writer until a merge, discard, abandon or
   cleanup releases it — see `spawn.lock_task_file` and "Ruling on a
   spawned task" in [docs/agents.md](agents.md#ruling-on-a-spawned-task).
4. **Worktree** — `_ensure_worktree()` creates
   `<worktreeRoot>/<project>-<taskid>` on branch `task/<taskid>` off the
   repo's current branch, or reuses an existing worktree/branch so a
   re-spawn after a killed session works. For a new worktree,
   `_link_worktree_paths()` adds the project's `worktreeLinks` exclusions
   to the common git `info/exclude`, verifies the destinations are untracked
   and ignored, then symlinks them to the configured main checkout. Missing
   sources or unsafe destinations become warnings, with no link created.
   Reused worktrees are left as-is. Worktree removal uses git's existing
   removal path, which unlinks symlinks without following their targets.
5. **tmux** — `tmux new-session -d -s centrale-<project>-<taskid> -c
   <worktree> -x 220 -y 200` launches the resolved agent command (with
   `CENTRALE_AGENT=<name>` and a per-task `CENTRALE_EVENT_URL` in its
   environment). The geometry is `spawn.SESSION_GEOMETRY`, and both
   creation paths — spawn and resume — build their argv through
   `spawn.new_session_args()` so neither can drift from the other. A
   detached `new-session` with no `-x/-y` inherits tmux's `default-size`
   (80x24) or, with a client attached to the same server, whatever that
   terminal happens to be, which made the pane capture's ceiling
   machine-dependent; an agent TUI owns the alternate screen and never
   accumulates scrollback, so that ceiling is the whole of what
   `/api/session-pane` can ever return (see that endpoint in
   [docs/api.md](api.md#get-apisession-paneprojectnametasktaskidlinesn)).
   Measured: 24 captured lines at 80x24, 50 at 220x50, 200 at 220x200 —
   the capture is the pane's height exactly, which is why the row count
   is `server.MAX_SESSION_PANE_LINES` rather than a second independent
   number: the pane is built exactly as tall as the largest window that
   endpoint will ever hand back, so the theater cannot ask for lines
   that can never arrive (task-152).
   `spawn.hold_session_geometry()` then does two things, both
   best-effort. It pins the session with `set-option window-size
   manual`, because the default (`latest`) lets the first client that
   attaches renegotiate the window down to its own terminal — and it
   stays there after that client detaches, so one `tmux attach` would
   otherwise undo this permanently for that session. And it issues an
   explicit `resize-window` *behind* the pin, because `window-size` is
   inherited globally and resolved when the session is born: with any
   client attached anywhere on the tmux server — the owner's own
   terminal always is — the `-x/-y` above is overridden at creation and
   `manual` then freezes the wrong size rather than preventing it
   (reproduced on an isolated socket with a 142x30 client attached:
   `new-session -x 220 -y 50` gave 142x29, the pin held 142x29, and only
   the resize reached 220x50). That is why the original passed on a
   clean socket and failed in real use. The trade-off is deliberate: an
   attached user on a smaller terminal sees only the part of the
   viewport that fits (undo it for one session with `tmux set-option -t
   <session>: window-size latest`). Both calls are best-effort — the
   session and agent are already live when they run, so a tmux too old
   for either (they arrived in the 2.9/3.0 generation) costs a smaller
   pane, not a failed spawn, and the resize is attempted even if the pin
   failed. Nothing ever resizes a session that already exists: the only
   target either call names is the session this spawn just created. Built-in command family is encoded as `agentKind` in that
   URL; the ephemeral event store uses it to expose codex turn-end as
   `idle` while preserving Claude's trustworthy `finished`.
6. **Agent** — the agent receives the standard workflow prompt ("Work on
   backlog task <ID>… check the repo's standing decision records… commit
   all your work on this branch"; `spawn.PROMPT_TEMPLATE`, replaceable
   per install via the top-level `spawnPrompt` key, validated by
   `server.normalize_spawn_prompt`), plus the agent's `promptSuffix` if
   configured. An optional `firstMessage` from the spawn request is appended
   to that same launch argument, so it is present before either a hub or
   remote worker starts; without it the prompt is unchanged (task-218).
   The agent works inside the worktree. The
   response gives the session name and `tmux attach -t ...` command; the
   sessions panel then shows the session and the files it touches.

## How a remote agent's status reaches Centrale

An agent's status badge (working, waiting, finished) comes from the agent
tool's own hooks, never from the model. Claude Code and codex run a hook
command on fixed events -- a prompt submitted, a tool about to run, a
request for input, the end of a turn -- and the model is not involved: it
cannot skip a hook or decide what it reports. Centrale hands the hook
commands to the tool when it starts it (`--settings` for claude, `-c`
overrides for codex; `spawn._inject_agent_hooks`), so nothing is written
into the user's config or the repository.

For an agent on this machine the hook writes its current state and firedAt
to its own tmux pane option, then calls Centrale over HTTP
(`centrale_notify.py` posting to `/api/agent-event`). HTTP remains the live
status path, including hook provenance and bounded retries; a failed pane
write does not prevent the POST. An agent on a worker
(a configured machine) cannot: nothing on the worker can reach Centrale.
Its hook does the one thing it can do locally instead: it writes a note on
its own tmux pane, the pane option `@centrale_state` set to
`"<state> <firedAt>"` (`server.hook_argv`'s `"tmux"` sink). Both are the
same generated command per event; only where it reports differs, and the
filter that ignores idle reminders is the same in both.

The hub reads those notes over ONE tmux control-mode connection per worker
(`tmux -C`, carried by the same ssh setup as every other remote command;
`remote_status.py`). tmux reports changes in pane options as they happen,
but only for panes in the session the control client is attached to, and
Centrale runs one session per task. So the connection attaches to a
dedicated session on the worker, `centrale_control`, and each task's window
is also linked into it (`link-window`; the window stays in its own task
session too). Each task pane is then watched by its pane id, and every
change goes through `record_agent_event`, exactly as an HTTP hook event
does, so the journal, the orchestrator-wait lines and codex's double-check
(which reads the pane over the same connection) behave the same. At spawn
the pane also gets its identity (`@centrale_project`, `@centrale_task`,
`@centrale_kind`), which every reported value carries, because tmux reuses
pane ids.

Why not a reverse tunnel, so the worker's hook could keep calling HTTP?
Centrale has no login: it relies on listening on 127.0.0.1 only. A tunnel
from the worker would put that whole API -- spawn, merge, discard, settings
-- within reach of anything running on the worker, every agent included.
And it would not survive restarts any better: a tunnel drops with the hub
or the network just as a control connection does, and events sent while it
is down are lost either way.

Why not a status file the hook writes on the worker? A file outlives the
agent that wrote it: an agent that crashed would keep saying "working"
forever, which is exactly the stale answer Centrale refuses to show. And
the hub would have to poll it. The pane note lives and dies with the pane,
and tmux pushes its changes.

If tmux on the worker crashes, the agent dies with it (it was running
inside it), and the note goes with the pane. Nothing stale is left to read:
the task shows as an ended session, with Resume, as after a worker reboot.
If the connection drops (network, ssh), the hub reconnects, re-links and
re-subscribes, and is sent each pane's current note; only intermediate
values written during the outage are missed.

When the hub restarts, it first connects to every worker and reads each
task pane's current note (waiting a bounded time, about 8 s per worker, in
parallel), restores those states as they stand, and only then announces
states to orchestrators -- so nobody hears "unknown" and then a silent
correction. A worker it cannot reach in time is announced as unreachable.
This is decision-8; with no `machines` configured none of it runs.

For every live local session, boot also reads its pane's current
`@centrale_state` before announcing local sessions. A valid note restores
its state, including `working`, with the hook's `firedAt` as `stateSince`;
it writes no journal row. Sessions with no valid note use decision-5's
settled-state journal restore. The pane option dies with its pane, so a
recreated session cannot inherit the prior agent's claim (task-223).

A successful Settings save of top-level `machines` reconciles those live
control connections (`remote_status.reconcile`): added or edited workers
get a new non-boot connection whose first scan reads the current panes;
removed workers stop and release their pane capture callbacks; unchanged
workers keep their subscriptions. A failed thread start is a Save warning
that names the worker and asks for a restart after the cause is fixed.

### Remote sessions in the sessions list and Fleet

`GET /api/sessions` and `get_fleet` start from the hub's own tmux survey
(`list_sessions`, the only thing `observe_fleet_sessions` sees: the
absence of a session `record_session_started` recorded as started on a
machine ends nothing, task-224) and then append
`remote_status.session_rows(config)` -- one row per task pane a reachable
machine's control connection holds, shaped like a local row plus `machine`,
`worktree`, `attach` and `reach`. A machine that is not reachable has no
panes to read, so its rows are the tasks `_journal_live_tasks` found last
seen live whose location the hub's fetched refs put there, marked
`unreachable`; `enrich_sessions_with_files` and the
Fleet agents turn those into `agentState`/`state` `"unreachable"` rather
than the journal's last state, and read no worktree for a remote row.
Pane captures (the PARKED check, the Needs-you permission dialog) go over
the control connection (`remote_pane_capture`), and the Fleet view groups
its cards by machine from `machines`, `hub` and where each project's live
agents run (each agent counted on its own session's machine, task-224).
`server.remote_session_rows` returns `[]` without importing
`remote_status` when no machines are configured, so a single-machine
response is key-for-key what it was.

## Testing

`python3 -m unittest discover tests` runs without tmux, network, or real
repos: the subprocess boundary functions are patched and board responses come
from recorded fixture JSON in `tests/fixtures/`. Coverage includes config
normalization, aggregation and per-project failure isolation, ready-flag
merging, session parsing without a tmux server, spawn validation rejections,
worktree reuse, and browser launch/reuse.

The frontend half of that suite has two tiers, described under
[The frontend files](#the-frontend-files): **source-shape** contracts that
grep `static/` through `tests/source_contract.py`, and **behavioural** tests
that drive the real sources under `node` over `tests/js_harness.py`. Which
tier a new frontend test belongs in is not a style preference:

- a claim about what the code DOES -- one poller, no fetch on open, this
  key closes that layer -- goes in `tests/test_frontend_behaviour.py`,
  because a substring is not evidence for it: it stays green through any
  behaviour change that preserves the spelling, and fires on a rename that
  changes nothing;
- a claim about the SHAPE of the source -- one home for an endpoint, no
  second copy of a node constructed anywhere, a file's load order -- stays
  textual, because a driven test can only speak for the flows it walks.

**`node` is optional in development and required for a release.** Without
it the behavioural tier skips whole and the suite still reports OK, which
is the intended behaviour on a development machine: nothing about
`python3 -m unittest discover tests` changed. It is not good enough for a
publication, because the source-shape tier only greps -- it never parses
or runs the JavaScript, so a frontend broken badly enough not to parse
used to pass the release gate on a machine with no `node` (task-124). So
`scripts/release.sh` requires `node` on the release machine (exit 3,
before anything is staged) and runs the staged snapshot's suite with
`CENTRALE_REQUIRE_NODE=1`, which turns `js_harness.requires_node`'s skip
into a failure for that run only. See
[The suite may not skip its way past a release](operations.md#the-suite-may-not-skip-its-way-past-a-release).

`tests/test_source_contract.py` holds the ratchet: it caps how many
source-text tests the suite has (106 today -- 148 before task-108 split
the tiers, 138 after it, 98 once task-118 drove the six families it had
left textual, 101 with task-107's three markup/CSS claims about the
footer's version credit, 103 with task-126's two cross-file counts over
the drawer's one `/api/task` call site, and 106 with task-98's three over
the header's two links) and fails if any fixture resolves a source slice,
which is what used to turn a rename into an error out of `setUpClass`.
That "today" figure is not kept by hand: it had gone stale twice by
task-163, each time in a raise that moved the constant and not the prose,
so the module now reads this sentence and fails if the number in it is
not the `SOURCE_TEXT_CEILING` being enforced -- raising the ceiling and
extending the history above are one edit. The same
module holds three censuses of this chapter: task-131's, that every
`tests/test_*.py` is named in the [module layout](#module-layout) table's
`tests/` row; task-150's, that every top-level `*.py` module has a row of
its own there; and task-141's, that
[The frontend files](#the-frontend-files) table lists exactly the
`static/*.js` files index.html loads, in that load order. Each one fails
the suite until the doc catches up -- those claims, not a docs linter.

`tests_integration/` is a separate, opt-in tier for real `git`/`tmux`/
`backlog` processes -- see its own README; `discover tests` never collects it.
Every external axis it touches is namespaced per test-run *process*, tmux
included: each run picks its own `-L centrale-itest-<pid>-<random>` socket
(overridable with `CENTRALE_ITEST_TMUX_SOCKET`) and asserts at teardown that
neither a server nor the socket file survives it, so two concurrent runs --
or a run and a release -- cannot kill or observe each other (task-161).
Opt-in in development, that is: a release runs it too. `scripts/release.sh`
requires `tmux` on the release machine and runs the staged snapshot's
`tests_integration/` after its `tests/`, with `CENTRALE_REQUIRE_INTEGRATION=1`
turning `base.require_tools`' skip into a failure for that run (task-130,
the same move as `node` one tier over) -- see the operations chapter's
[Releasing](operations.md#releasing) section.

See also: the [Multi-agent workflow](agents.md#multi-agent-workflow)
section of the agents chapter for the operator-facing loop these modules support.
