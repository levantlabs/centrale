# Centrale architecture

Centrale is a local, stdlib-only web dashboard that aggregates Backlog.md task
boards from multiple repos into one kanban view and can spawn a coding agent
(Claude Code by default) on a task inside a git worktree. It stores no data of
its own: every repo's `backlog/` folder stays the source of truth, read through
the `backlog` CLI (`--json` everywhere it exists — see the milestone-title
exception in [docs/api.md](api.md#get-apiboard)'s field list), never by parsing
task markdown directly. The server binds `127.0.0.1` only.
[docs/api.md](api.md) is the authority on the HTTP contract — every route, a
real response for each, and every error status; this chapter covers the module
layout behind it.

## Module layout

| Path | Role |
|---|---|
| `server.py` | Backend: config loading, board aggregation, subprocess boundaries, HTTP server + routing |
| `spawn.py` | Worktree + tmux spawn engine behind `POST /api/spawn` |
| `harvest.py` | Gated merge engine behind `POST /api/harvest` (and the live `GET /api/harvest-progress` record) |
| `settings.py` | Settings read/write behind `/api/settings` — a whitelisted subset of `projects.json`, including (task-78) the whole `agents` map for the modal's Agents editor |
| `browser.py` | Per-project `backlog browser` launcher behind `POST /api/browser` |
| `version.py` | `__version__`, and nothing else — the one place the version number is written down (task-107). Imports nothing and does nothing at import, so `scripts/release.sh` can read it out of a bare staged snapshot to derive the release tag; `server.py` imports it for what `--check` and the sidebar footer report; `code_drift()` there compares that boot-time version with `HEAD` on every board request and reports `codeDrift` (task-128), which `--check` relays from a running server through the `probe_running_server` boundary. See [Versioning](operations.md#versioning) |
| `centrale_notify.py` | The agent-lifecycle hook helper behind the session dots (task-37). Injected transiently onto a spawned session -- into a generated Claude Code hooks settings file, or codex's `notify` override -- never written into the user's home config or the target repo. Run with one of `working`, `waiting`, `finished`, it POSTs that state to `$CENTRALE_EVENT_URL` (`spawn.event_url`) for `POST /api/agent-event`. Every failure -- bad argv, unset URL, network error, timeout -- is swallowed silently, because a hook must never break or hang the agent it is attached to |
| `static/index.html` | The frontend HTML document shell (single page, no framework/build step) |
| `static/*.js` | The frontend JS, one file per concern, each loaded by its own plain `<script src>` at the end of index.html's body in a fixed order (task-83 extracted it from index.html, task-89 split it up) — see [The frontend files](#the-frontend-files) |
| `static/styles.css` | All the frontend CSS, loaded by a plain `<link>` from index.html (task-82) |
| `static/favicon.svg` | The tab icon, declared by a `<link rel="icon">` in index.html and also served at `/favicon.ico` (task-90) |
| `projects.json` | User config: port, worktree root, projects, agents map, browser ports |
| `tests/` | The default `unittest` suite, one module per concern -- `test_server.py` (config loading, board aggregation, sessions, the agent-event store and hook injection, the HTTP routes and their request gate, and the source-shape contracts over `static/`); `test_spawn.py` (worktree/branch resolution, agent selection, the claim commit, resume and reconcile, the spawn/resume validation refusals); `test_harvest.py` (each merge gate, branch evaluation, the harvest lock and progress record, auto-harvest, `/api/harvest`); `test_settings.py` (the whitelisted read/write of `projects.json`, the Agents editor, `/api/settings`); `test_browser.py` (per-project `backlog browser` launch, reuse, relaunch, the listener-pid registry and its register-time reconciliation, and the board-version comparison); `test_frontend_behaviour.py` (what the frontend DOES, driven under `node`); `test_source_contract.py` (the source-text tier's own machinery, the source-text ceiling, and the three censuses over this chapter -- that every module here is named in this table (task-131), that every top-level `*.py` module has a row of its own in it (task-150), and that [The frontend files](#the-frontend-files) lists the `static/*.js` files in load order (task-141)); `test_doctor.py` (the `--check` doctor's pass/warn/fail verdicts and its CLI wiring); `test_version.py` (the one version constant, how the server resolves it, and the tag `scripts/release.sh` derives from it); `test_centrale_notify.py` (the `centrale_notify.py` hook helper's argv gating and its POST to `/api/agent-event`); `test_scan_release.py` (`scripts/scan_release.py`'s secret and identity scan, including a clean run over this repository); `test_release_snapshot.py` (what `git archive HEAD` ships: source in, `backlog/` out, no generated content); `test_docs_operations.py` (docs/operations.md's hand-kept tool and allowlist enumerations, pinned to release.sh, `tests_integration/` and the scan -- task-145); `test_docs_references.py` (the one rule over every document at once: a path under `tests/` or `tests_integration/` named in the public prose or a top-level module has to resolve to a file, after version.py cited a test that never existed -- task-163); `test_screenshots_script.py` (the one property `scripts/screenshots.py` may not lose: its faked CLI boundaries refuse any directory outside the throwaway sandbox, and no command-line option can aim it at a real board -- hermetic, and Playwright-free like everything else here -- task-153); `test_integration_base.py` (the unit-tier test OF `tests_integration/base.py`: its pure `parse_created_task_id` helper, `require_tools`' skip-or-fail switch on `CENTRALE_REQUIRE_INTEGRATION`, and the per-run tmux socket name and its path -- all decided before any subprocess runs, so it runs no integration test and launches nothing) -- plus recorded JSON fixtures in `fixtures/` and two shared helpers: `source_contract.py` (lazy, self-describing slices of `static/`) and `js_harness.py` (the DOM shim the frontend is driven over under node). Every `test_*.py` here must be named in this row: `test_source_contract.py` fails the suite otherwise |
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
- **Board aggregation** — `get_board()` fans out per project on a
  `ThreadPoolExecutor`, merging `task list --json` with `task list --ready
  --json` to set each task's `ready` flag, with a ~5s in-memory cache. A
  failing project becomes an `error` field on that project, never a crash.
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
  the last 10s), `POST /api/spawn`, `POST /api/browser`. Errors are JSON `{"error": ...}`;
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
  the only cross-task lock is harvest's own `_harvest_lock`, and the order is
  fixed — `harvest_branch` takes `_harvest_lock` *then* the per-task lock, and
  nothing holding the per-task lock ever asks for `_harvest_lock`, so there is
  no cycle. Read-only routes take neither. The destructive throwaway routes
  additionally require the state their confirm described
  (`expectedBranchTip`, `expectedDirtyPaths`, straight from `GET
  /api/discard-preview`) and refuse with 409 if a fresh survey under the lock
  disagrees — see [docs/api.md](api.md#post-apidiscard-attempt).

`spawn.py`, `harvest.py`, `settings.py` and `browser.py` import `server` for
these boundaries and are themselves imported lazily at their call sites,
avoiding a circular import at module load. They call `server.run_git(...)`
through the module object so a single patch is seen everywhere.

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

### browser.py

`launch_or_reuse()` starts a detached `backlog browser` process per project on
a stable port (`browserPortBase` + project index, or per-project
`browserPort`), reuses it while alive, and terminates all of them atexit.
`version_drift()` then asks the board that was opened what version it is
running (`/api/version` on that same port) and compares it with `backlog
--version` on `PATH`, so the UI can say when a board is behind the CLI --
reported, never acted on.

### The frontend files

Fourteen plain `<script src>` files, loaded in this order at the end of
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
| `main.js` | The top-level render orchestration and the boot call |

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
   already exists.
2. **Claim** — `_claim_and_commit()` sets the task to "In Progress" via
   `backlog task edit` in the main repo, then…
3. **Commit** — commits any resulting `backlog/` change on the repo's current
   branch, so the worktree cut next already contains the claim (avoiding a
   divergent copy of the task file when the agent claims it again). Both
   steps are best-effort: failures become `warnings` in the response, never a
   blocked spawn.
4. **Worktree** — `_ensure_worktree()` creates
   `<worktreeRoot>/<project>-<taskid>` on branch `task/<taskid>` off the
   repo's current branch, or reuses an existing worktree/branch so a
   re-spawn after a killed session works.
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
   configured, and works inside the worktree. The
   response gives the session name and `tmux attach -t ...` command; the
   sessions panel then shows the session and the files it touches.

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
