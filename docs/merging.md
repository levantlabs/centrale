# Centrale merging

Merging finished branches: the six safety gates, the harvest API, the
card/drawer states, auto mode, and merge ownership. Part of the Centrale
manual; the [README](../README.md) is the front door and lists the other
chapters.

## Merging finished branches

Once a spawned agent finishes a task, its `task/<id>` branch just sits there
until someone merges it. Merging automates that, but only behind six
explicit safety gates (implemented in `harvest.py` — the module, config
key, and API keep that name; everything user-facing calls it Merge): five
that decide whether a branch is offered as ready to merge at all, plus a
sixth that only ever runs immediately before an actual merge attempt (see
below). By default (`harvest.mode` unset or `"click"`) merging only ever
runs when you click something; see "Auto mode" below for the alternative.

A branch is **harvestable** (the API's field name for "ready to merge")
only when all five gates pass, evaluated in
order (evaluation stops at the first failure — later gates are reported as
"not evaluated" rather than run, since e.g. there's no point dry-running a
merge for a task that isn't even Done):

1. **No live session** — no `centrale-<project>-<taskid>` tmux session is
   still running (the agent has actually exited).
2. **Task done** — `backlog task view <id> --json` reports status `Done` and
   every acceptance criterion checked from the **task branch**, never the
   project's main checkout. With a Centrale worktree it runs there as before;
   with a parked branch it runs in a disposable detached snapshot of the
   branch. A foreign checkout still fails cleanly, naming "checked out outside
   Centrale at `<path>`", because recoverable work may still be in progress
   there. This keeps an agent's branch-only Done status, checked ACs, and
   implementation notes authoritative until merge, and lets Backlog itself
   resolve spaced task filenames in either source. See "Branches worked
   outside Centrale" in [docs/agents.md](agents.md#branches-worked-outside-centrale) and "Using the
   board" in [docs/board.md](board.md#using-the-board) for the rendered distinctions.

   If this branch-side gate fails while the board/main-checkout task says
   `Done`, Centrale reports the divergence explicitly instead of claiming
   the visible card is simply wrong: both statuses, whether the main task
   file is uncommitted, its exact path, and the agent branch's last commit
   subject are returned. The drawer then offers a two-click informed
   confirm to adopt the board Done onto the branch. On confirmation Centrale
   requires a clean agent worktree, runs `backlog task edit -s Done` there,
   commits only that task path, and re-runs every gate before merging. It
   does not auto-check acceptance criteria, so an unchecked criterion still
   blocks normally after adoption. Without the exact main-Done/
   branch-non-Done divergence, gate 2's reason and behavior are unchanged.

3. **Worktree clean** — when the branch has a Centrale worktree, `git status
   --porcelain` in it must be empty as before. When a fresh, fail-closed `git
   worktree list --porcelain` proves the branch is parked, this gate passes
   vacuously with "no worktree -- nothing uncommitted to protect". Any
   foreign or stale Centrale checkout still refuses; failure to enumerate
   worktrees also refuses rather than guessing parked. In both cases the
   branch must have at least one commit `main` doesn't already have (a branch
   with nothing new reports "nothing to merge" distinctly from a real
   failure).
4. **Merge clean** — a dry-run merge, done in a throwaway *temporary*
   worktree (never the project's own checkout), shows no conflicts.
5. **checkCommand** — if the project sets one (see `checkCommand` in [docs/configuration.md](configuration.md#setup--configuration)),
   it's run against that same merged temporary worktree and must exit 0
   within `checkTimeoutSeconds` (default 600s). A project without one
   skips this gate — it passes vacuously. A timeout is just another way
   this gate fails, with its own clear reason ("checkCommand timed out
   after Xs") — never a request that hangs waiting on it.

When gate 4 or 5 fails on a branch that has meanwhile fallen behind its
base, the report also says how far behind and hints that the failure may be
a collision with newer work rather than a defect in the branch, and the
drawer offers to send the agent back to reconcile — see "Resume to
reconcile" under "Resuming an interrupted agent" in [docs/agents.md](agents.md#resume-to-reconcile-a-branch-that-fell-behind-main). Gates 1–3 failing,
or a branch that's up to date, never carry that hint.

The parked gate-2 read and gates 4/5 use disposable, detached worktrees under
the standard `TMPDIR` location: gate 2's snapshot is cut from the task branch
only long enough to run Backlog, while gates 4 and 5 share a separate snapshot
cut from the project's current branch. The task branch is merged into the
latter, and (if that's clean) `checkCommand` runs there too — all before the
project's real checkout is touched at all. Every scratch worktree is removed
afterward regardless of outcome. Only once all five pass does Centrale run the
sixth and final gate — `mainCheckoutClean`, which only
ever appears in a `POST /api/harvest` response, never in `GET`'s read-only
evaluation (see [docs/api.md](api.md#post-apiharvest)) — immediately before the real
merge: whether the project's own checkout has picked up anything that would
actually conflict with this merge since gates 4/5 ran (they never look at
it). This check tolerates **unrelated** uncommitted dirt rather than refusing on any
uncommitted change at all, matching git's own real merge behavior (verified
empirically, not assumed, while building this): it computes the set of
paths this merge would touch (`git diff --name-only`) and the set of
uncommitted paths in the checkout (`git status --porcelain`, both unstaged
modifications and untracked files), and only refuses when they actually
overlap — an untracked file sitting at a path the branch would create
counts as overlapping too, the same way git itself refuses to let a merge
silently overwrite one. The one exception: a **staged** change (`git add`ed
but not committed) blocks the merge unconditionally, even to a path the
incoming branch's diff never touches at all — this isn't Centrale being
extra cautious, it's git's own `--no-ff` merge itself refusing outright the
moment the index disagrees with `HEAD` at all, regardless of which path. A
refusal names every blocking file and suggests the fix: `"<project>'s
checkout has uncommitted changes that would conflict with this merge --
commit or stash these N files: <list>"`. If Centrale can't determine what
the merge would touch at all (a `git diff` failure), it refuses
conservatively rather than guessing nothing overlaps. (git wraps a path
containing a space — which every Backlog.md task filename has — or other
special character in double quotes with C-style escaping the instant it
prints one; `git status --porcelain` and `git diff --name-only` don't
always agree on when to do this for the very same path, so every path this
overlap check reads is first run through the same dequoting Centrale
applies everywhere else a git path gets parsed, or the comparison silently
never matches at all.)

There is one confined follow-up for the redundant board edit created by the
Done divergence: when the *only blocking path* is an unstaged tracked edit
to that task's own file, the drawer offers a separate two-click confirm that
names the exact path. Confirmation re-checks the condition, runs
`git restore --worktree -- <exact-path>` for that path only, then re-runs
all gates and merges only if green. A staged, untracked, renamed, or
additional blocking path keeps the ordinary refusal; nothing is stashed or
forced and no other path is touched.

When the merge proceeds with unrelated dirt still present, the response
notes how many
files were left untouched (see the API section below) — nothing is stashed
or moved to make room for it, it's simply not in the merge's way. Only
then: merge into the project's current branch (`git merge --no-ff`, message
`Merge task/<id>: <task title>`), remove the Centrale worktree when one
exists, and delete the branch with `git branch -d` (never `-D`). A parked
branch simply skips the nonexistent managed-worktree removal. A merge failure
never leaves a half-finished merge behind (`git merge --abort` runs
automatically); a worktree/branch cleanup failure after
an already-successful merge is reported as a non-fatal warning, not treated
as the merge having failed, and `-d` leaves a branch intact if git cannot
prove it is merged.

Before any of the five gates runs at all, evaluation first checks whether
the branch itself still exists. If it doesn't — already merged by this
attempt racing another, or removed by hand — that's reported as a distinct
`"alreadyMerged": true` result (`"harvestable": false`, `"gates": []`)
rather than a gate failure, since there's nothing left to evaluate. Without
this, a stale card (or two clicks racing each other) would surface a
confusing gate-2 "worktree not found" message instead of a plain "there's
nothing here" — the UI treats `alreadyMerged` as success-shaped (an info
toast, not an error) rather than a blocked result.

**API:**

- `GET /api/harvest?project=<name>` evaluates every `task/<id>` branch in a
  project's gate status and returns it — read-only, never merges, never
  touches the project's own checkout (only the detached snapshots a parked
  gate-2 read and gates 4/5 need, each cleaned up immediately). Responds
  `{"branches": [{"taskId", "branch", "harvestable", "taskTitle",
  "gates": [{"name", "passed", "reason"}, ...]}, ...], "events": [...]}` —
  `passed` is `null` for a gate that wasn't reached; a branch that no
  longer exists reports `"alreadyMerged": true` and an empty `"gates"`
  instead. A branch blocked at `mergeClean`/`checkCommand` that is behind
  its base additionally carries `"behindBase": {"baseBranch", "count"}` and
  a `"reconcileHint"` string (see "Resume to reconcile" in [docs/agents.md](agents.md#resume-to-reconcile-a-branch-that-fell-behind-main)). `events` is the in-memory log of that project's recent merge
  *attempts* (both clicked and auto), newest included, each `{"time",
  "project", "taskId", "branch", "trigger": "click"|"auto", "merged",
  "baseBranch"?, "alreadyMerged"?, "reason"?, "error"?}` — see "Auto mode"
  below.
- `POST /api/harvest` with `{"project": "<name>", "taskId": "<id>"}` runs
  the full gate check for one branch and merges it if every gate passes.
  With `{"project": "<name>", "all": true}` instead of `taskId`, it
  merges every branch in that project that's currently ready, one at a
  time — each one is re-evaluated fresh immediately before it's merged
  (via the same one-branch path), since merging one branch moves the base
  branch forward and can change whether a *later* branch's merge is still
  clean. Responds with the one-branch report (plus `"merged": true/false`,
  and on success `"baseBranch"`, optionally `"warnings"`, and optionally
  `"unrelatedDirtyCount"` — the number of uncommitted files left untouched
  in the checkout because they didn't overlap with this merge, omitted
  entirely rather than `0` when there were none — or `"alreadyMerged": true`
  if the branch was already gone), or `{"merged": [...], "notReady": [...]}`
  for the `all` form (an already-merged branch, if raced away between
  listing and evaluating, lands in `notReady` — it wasn't actively merged
  by this call).
- `GET /api/harvest-progress` names the gate the one in-flight merge is on
  right now — `{"progress": {"project", "taskId", "branch", "trigger",
  "gate"}}`, or `{"progress": null}` when nothing is being merged. It's what
  the Merge button polls while its own POST is outstanding (see the UI
  section below). One in-memory read, no subprocess: the record lives only
  for the duration of an attempt and is cleared on every terminal outcome,
  so a finished merge leaves nothing readable behind. `POST /api/harvest` is
  the only writer, so clicks, `all: true` passes and auto-harvest cycles all
  report through this one record; `GET /api/harvest` publishes nothing.

**UI:** a task's card and drawer action follows the same branch lifecycle
`hasSpawnBranch` (see [docs/api.md](api.md#get-apiboard)) and live-session state track,
never `main`'s task status — a spawn's own Done/AC/notes updates only exist
on its branch until merged (the same reason gate 2 reads from the worktree,
above), so main's status is not a reliable signal for any of this:

1. No branch, no live session — "Spawn agent" (or "Spawn (\<agent\>)"), as
   described in [docs/agents.md](agents.md#spawning-an-agent).
2. A live session — "Session live" (disabled), regardless of whether a
   branch also exists yet. A branch's own actions don't vanish while its
   agent runs, they go grey: the drawer still shows Merge and the
   throwaway actions below, disabled and naming the session that blocks
   them — Merge could only fail gate 1 there, and the two throwaway
   routes refuse with a 409 (see "Ending a session" in
   [docs/agents.md](agents.md#ending-a-session)).
3. A branch, no live session (**awaiting merge**) — the card shows "Merge"
   as the only action; the drawer shows Merge as the primary action plus
   one row of subtler ones under it, drawer-only and confirm-armed. One
   or two of them are for deliberately sending an agent back into that
   same worktree instead of starting fresh somewhere new: a branch that
   looks finished — clean worktree, and its own copy of the task already
   `Done` — gets just "Re-spawn agent" (fresh prompt, e.g. to fix a
   failed safety gate), while anything that looks **interrupted** —
   uncommitted changes in the worktree, or a branch-side task still in an
   active status — leads with "Resume agent", continuing that same
   session's conversation, and keeps Re-spawn beside it as the quieter
   alternative (see "Resuming an interrupted agent" in [docs/agents.md](agents.md#resuming-an-interrupted-agent)). The row also
   carries the ways out of a bad attempt — "Discard attempt", and
   "Abandon worktree, keep branch" when there is a Centrale worktree to
   abandon (see "Discarding a bad attempt" in [docs/agents.md](agents.md#discarding-a-bad-attempt)). Those two are
   left out entirely while this task's own Merge POST is still
   outstanding: the gates are running against the very worktree and
   branch they would delete. A card never shows both Spawn and Merge at
   once, and the drawer never shows a plain Spawn alongside either
   Re-spawn or Resume.
4. After a merge (no branch again) — back to state 1 if the task isn't
   Done, or no action at all once it is. This transition is instant: the
   Merge/Re-spawn/Resume button and the "unmerged branch"/"interrupted"
   badge all disappear the moment a merge (or "already merged") response
   comes back, in that same render, rather than waiting on the next board
   refresh to notice — a success confirmation line still shows in their
   place for that task, and a board refetch is still kicked off
   immediately afterward to pick up everything else the merge changed.

Clicking Merge posts to `/api/harvest` and shows the result inline — a
success line naming what got merged, or the first failing gate's reason
(hover the line for the full per-gate breakdown); the five gates, not the
button's visibility, are what decide whether a merge actually happens.

**While that POST is open, the button says which gate is running.** On a
project with a `checkCommand` the merge is dominated by that one gate — the
other six stages are milliseconds each, and the test suite is essentially
the whole wait — so the button reads "Running tests…" for it, and
"Checking session…"/"Checking task…"/"Checking worktree…"/"Trial merge…"/
"Checking checkout…"/"Merging…" for the stages around it. The name always
comes from the server (`GET /api/harvest-progress`, polled once a second
while, and only while, this client's own merge request is outstanding); it is
never inferred client-side from the known gate order, so with nothing to
report — an early poll, another project's merge holding the lock — the button
keeps its plain "Merging…". A project without a `checkCommand` merges in well
under half a second, finishing before the first poll is even due, so nothing
about it changes. No elapsed time, estimate, countdown or percentage is shown
anywhere: which gate is running is the useful fact. "Merge all ready" shows
the same label for whichever project its pass is currently walking.
Clicking it on a branch that's already gone (see `alreadyMerged` above)
shows a plain "Already merged" line instead of a gate-failure message. The
sidebar's Sessions section has a "Merge all ready" button that runs the
`all: true` form across every configured project, one project at a time,
and reports how many branches were merged — each merged task's card/drawer
also transitions instantly, the same as a single click.

**Auto mode:** with `harvest.mode` set to `"auto"` in `projects.json`, a
daemon thread started alongside the server (`harvest.AutoHarvestThread`)
wakes up roughly every 30 seconds, re-reads the configured mode (so
flipping it back to `"click"` takes effect on the thread's next wake-up,
no restart needed), and — only while still `"auto"` — evaluates and
merges every ready branch in every configured project, through the exact
same `evaluate_branch`/`harvest_branch` code path a click uses (no separate
merge logic, no separate gate logic). A single lock serializes auto-merge
cycles with click-triggered merges so the two can never interleave into
the same merge. Gate 1 ("no live session") still evaluates correctly with
tmux unavailable — only the session lookup itself degrades (see "Without
tmux" in [docs/agents.md](agents.md#without-tmux)), not the rest of gate evaluation.

Every merge *attempt*, clicked or automatic, appends one entry to a
bounded (50 most recent) in-memory event log, returned as `events` in
`GET /api/harvest` (see the API section above). The frontend polls that
endpoint on its normal 10-second refresh cycle — but only while
`GET /api/board`'s `harvestMode` field reads `"auto"`, so click-mode
installs never pay for extra polling — and shows a toast for any new
`trigger: "auto"` event it hasn't shown yet: a success toast naming what got
merged, or an error toast if the attempt itself failed unexpectedly, and
that task's card/drawer transitions instantly too, the same as a click
(see state 4 above). Routine
gate-blocked auto-attempts (nothing was ready yet) are logged but not
toasted, since most cycles don't find anything ready and a toast every 30
seconds for that would be noise. The very first poll after auto mode is
noticed only records a baseline and toasts nothing, so switching to auto
mode doesn't dump the pre-existing event history onto screen.

**Merge ownership:** the six gates above only certify a merge Centrale
itself performs. A spawned agent's prompt explicitly tells it not to merge
or delete its own `task/<id>` branch — that's the dashboard's job — but
Centrale has no way to enforce this beyond asking: an agent (or a human)
merging a `task/*` branch directly, outside Centrale, is not blocked and not
detected as an error. It's supported and safe either way — every gate and
every board state re-derives fresh from git/backlog/tmux on each read, so
Centrale never gets confused by a branch that's already gone — but a merge
that happens outside Centrale was never checked against those six gates
(including `checkCommand` running against the *merged* tree), so it's
un-certified: nothing here can tell you it passed a check it never ran.
